"""Shellie-owned FrontDesk sales ledger and PayPal webhook processing.

This module is deliberately separate from ``paypal.py``.  ``paypal.py`` is for
payments that a FrontDesk customer performs in their own business.  This module
uses only ``SHELLIE_PAYPAL_*`` settings and stores only Shellie's purchasers.

The safe default is a tax hold.  A completed PayPal capture is recorded, but no
download entitlement becomes active until tax handling has either been supplied
by the payment record or approved by an authorised Shellie operator.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "data" / "shellie-sales.db"
SCHEMA_VERSION = 1
SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"
TOKEN_VERSION = "FD1"

PLANS = {
    "frontdesk-lifetime": {
        "name": "FrontDesk",
        "amount": "2999.00",
        "currency": "USD",
        "payment_link": "https://www.paypal.com/ncp/payment/WZMRKU9FRKW4S",
        "license": "packaged delivery and support; source remains Apache-2.0",
    }
}


class SalesError(RuntimeError):
    pass


class SalesConfigurationError(SalesError):
    pass


class SalesVerificationError(SalesError):
    pass


class SalesClaimError(SalesError):
    pass


_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def database_path() -> Path:
    configured = os.environ.get("SHELLIE_SALES_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DATABASE


def connect(database: Path | None = None) -> sqlite3.Connection:
    target = database or database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10, factory=_Connection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sales_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        received_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sales_orders (
        order_id TEXT PRIMARY KEY,
        capture_id TEXT UNIQUE,
        plan_id TEXT NOT NULL,
        status TEXT NOT NULL,
        amount TEXT NOT NULL,
        currency TEXT NOT NULL,
        buyer_email TEXT NOT NULL,
        buyer_country TEXT NOT NULL,
        tax_amount TEXT NOT NULL,
        tax_currency TEXT NOT NULL,
        tax_status TEXT NOT NULL,
        refund_status TEXT NOT NULL,
        dispute_status TEXT NOT NULL,
        receipt_id TEXT NOT NULL UNIQUE,
        paid_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sales_entitlements (
        order_id TEXT PRIMARY KEY REFERENCES sales_orders(order_id),
        status TEXT NOT NULL,
        issued_at REAL,
        revoked_at REAL,
        claim_count INTEGER NOT NULL DEFAULT 0,
        last_claimed_at REAL
    );
    CREATE TABLE IF NOT EXISTS sales_actions (
        action_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    """)
    connection.execute(
        "INSERT INTO schema_meta(key,value) VALUES('version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def paypal_base_url() -> str:
    return LIVE_BASE if os.environ.get("SHELLIE_PAYPAL_ENV", "sandbox").lower() == "live" else SANDBOX_BASE


def paypal_is_configured() -> bool:
    return all(os.environ.get(name) for name in (
        "SHELLIE_PAYPAL_CLIENT_ID", "SHELLIE_PAYPAL_CLIENT_SECRET",
        "SHELLIE_PAYPAL_WEBHOOK_ID",
    ))


def _request(method: str, path: str, headers: dict[str, str], body: bytes | None) -> dict:
    request = urllib.request.Request(paypal_base_url() + path, data=body,
                                     headers=headers, method=method)
    try:
        # paypal_base_url returns one of the two module-fixed PayPal HTTPS origins.
        with urllib.request.urlopen(request, timeout=60,  # nosec B310
                                    context=ssl.create_default_context()) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SalesError(f"Shellie PayPal API error {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise SalesError(f"Could not reach Shellie PayPal API: {exc.reason}") from None
    return json.loads(raw) if raw else {}


def _access_token() -> str:
    client_id = os.environ.get("SHELLIE_PAYPAL_CLIENT_ID", "")
    client_secret = os.environ.get("SHELLIE_PAYPAL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise SalesConfigurationError("Shellie PayPal application credentials are not configured.")
    now = time.monotonic()
    if _token_cache["token"] and now < float(_token_cache["expires_at"]):
        return str(_token_cache["token"])
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    result = _request(
        "POST", "/v1/oauth2/token",
        {"Authorization": f"Basic {credentials}",
         "Content-Type": "application/x-www-form-urlencoded"},
        b"grant_type=client_credentials",
    )
    token = result.get("access_token")
    if not token:
        raise SalesConfigurationError("Shellie PayPal did not return an access token.")
    _token_cache.update(token=token,
                        expires_at=now + max(0, int(result.get("expires_in", 0)) - 60))
    return str(token)


def _authed(method: str, path: str, payload: dict | None = None,
            idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_access_token()}",
               "Content-Type": "application/json"}
    if idempotency_key:
        headers["PayPal-Request-Id"] = idempotency_key
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    return _request(method, path, headers, body)


def verify_webhook(headers: dict[str, str], event: dict) -> bool:
    """Ask PayPal to verify its signature before the event reaches the ledger."""
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    required = {
        "auth_algo": "paypal-auth-algo",
        "cert_url": "paypal-cert-url",
        "transmission_id": "paypal-transmission-id",
        "transmission_sig": "paypal-transmission-sig",
        "transmission_time": "paypal-transmission-time",
    }
    missing = [header for header in required.values() if not lowered.get(header)]
    webhook_id = os.environ.get("SHELLIE_PAYPAL_WEBHOOK_ID", "").strip()
    if missing or not webhook_id:
        return False
    payload = {field: lowered[header] for field, header in required.items()}
    payload.update(webhook_id=webhook_id, webhook_event=event)
    result = _authed("POST", "/v1/notifications/verify-webhook-signature", payload)
    return result.get("verification_status") == "SUCCESS"


def get_order(order_id: str) -> dict:
    return _authed("GET", f"/v2/checkout/orders/{order_id}")


def _money(value: object) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise SalesVerificationError("PayPal event contains an invalid amount.") from None
    if not amount.is_finite() or amount < 0:
        raise SalesVerificationError("PayPal event contains an invalid amount.")
    return format(amount, "f")


def _capture_ids(resource: dict) -> tuple[str, str]:
    capture_id = str(resource.get("id") or "").strip()
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    order_id = str(related.get("order_id") or "").strip()
    if not capture_id or not order_id:
        raise SalesVerificationError("PayPal capture is missing its capture or order id.")
    return capture_id, order_id


def _buyer(order: dict) -> tuple[str, str]:
    payer = order.get("payer") or {}
    email = str(payer.get("email_address") or "").strip().casefold()
    address = payer.get("address") or {}
    country = str(address.get("country_code") or "").strip().upper()
    if not email:
        raise SalesVerificationError("The PayPal order has no buyer email for fulfilment.")
    return email, country


def _tax(order: dict) -> tuple[str, str]:
    units = order.get("purchase_units") or []
    amount = (units[0].get("amount") or {}) if units else {}
    tax = (amount.get("breakdown") or {}).get("tax_total") or {}
    return _money(tax.get("value", "0")), str(tax.get("currency_code") or "").upper()


def _audit_action(connection: sqlite3.Connection, order_id: str, actor: str,
                  action: str, detail: str) -> None:
    connection.execute(
        "INSERT INTO sales_actions VALUES(?,?,?,?,?,?)",
        (f"SA-{secrets.token_hex(10)}", order_id, actor, action, detail, time.time()),
    )


def _record_completed(event: dict, resource: dict, order: dict,
                      connection: sqlite3.Connection) -> dict:
    plan_id = "frontdesk-lifetime"
    plan = PLANS[plan_id]
    capture_id, order_id = _capture_ids(resource)
    amount = _money((resource.get("amount") or {}).get("value"))
    currency = str((resource.get("amount") or {}).get("currency_code") or "").upper()
    if amount != plan["amount"] or currency != plan["currency"]:
        raise SalesVerificationError(
            f"Capture amount {amount} {currency} does not match {plan['amount']} {plan['currency']}.")
    merchant_expected = os.environ.get("SHELLIE_PAYPAL_MERCHANT_ID", "").strip()
    merchant_received = str((resource.get("payee") or {}).get("merchant_id") or "").strip()
    if merchant_expected and not hmac.compare_digest(merchant_expected, merchant_received):
        raise SalesVerificationError("Capture belongs to a different PayPal merchant.")
    buyer_email, buyer_country = _buyer(order)
    tax_amount, tax_currency = _tax(order)
    tax_mode = os.environ.get("SHELLIE_TAX_MODE", "manual").strip().lower()
    if tax_mode not in {"manual", "paypal-reported", "preapproved"}:
        raise SalesConfigurationError("SHELLIE_TAX_MODE must be manual, paypal-reported, or preapproved.")
    if tax_mode == "preapproved":
        tax_status = "approved"
    elif tax_mode == "paypal-reported" and Decimal(tax_amount) > 0:
        tax_status = "paypal_reported"
    else:
        tax_status = "manual_review"
    entitlement = "active" if tax_status in {"approved", "paypal_reported"} else "held_tax"
    now = time.time()
    receipt_id = "FD-" + hashlib.sha256(order_id.encode()).hexdigest()[:12].upper()
    connection.execute(
        "INSERT INTO sales_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(order_id) DO UPDATE SET capture_id=excluded.capture_id," 
        "status=excluded.status,updated_at=excluded.updated_at",
        (order_id, capture_id, plan_id, "paid", amount, currency, buyer_email,
         buyer_country, tax_amount, tax_currency or currency, tax_status,
         "none", "none", receipt_id, now, now),
    )
    connection.execute(
        "INSERT INTO sales_entitlements VALUES(?,?,?,NULL,0,NULL) "
        "ON CONFLICT(order_id) DO UPDATE SET status=excluded.status," 
        "issued_at=COALESCE(sales_entitlements.issued_at,excluded.issued_at),revoked_at=NULL",
        (order_id, entitlement, now if entitlement == "active" else None),
    )
    _audit_action(connection, order_id, "paypal", "capture_completed",
                  f"event={event.get('id')} tax={tax_status}")
    return {"order_id": order_id, "capture_id": capture_id,
            "status": "paid", "entitlement": entitlement, "tax_status": tax_status}


def _order_for_event(resource: dict, connection: sqlite3.Connection) -> sqlite3.Row | None:
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    capture_ids = [
        str(related.get("capture_id") or "").strip(),
        str(resource.get("id") or "").strip(),
    ]
    for disputed in resource.get("disputed_transactions", []):
        transaction = disputed.get("seller_transaction_id") or (
            disputed.get("transaction_info") or {}).get("seller_transaction_id")
        capture_ids.append(str(transaction or "").strip())
    for capture_id in capture_ids:
        if capture_id:
            row = connection.execute(
                "SELECT * FROM sales_orders WHERE capture_id=?", (capture_id,)).fetchone()
            if row:
                return row
    order_id = str(related.get("order_id") or "").strip()
    if order_id:
        return connection.execute("SELECT * FROM sales_orders WHERE order_id=?", (order_id,)).fetchone()
    return None


def process_webhook(event: dict, *, database: Path | None = None,
                    order_loader: Callable[[str], dict] = get_order) -> dict:
    """Persist one already-verified PayPal event exactly once."""
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("event_type") or "").strip().upper()
    resource = event.get("resource") or {}
    if not event_id or not event_type or not isinstance(resource, dict):
        raise SalesVerificationError("PayPal event is missing required fields.")
    payload_hash = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    connection = connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        seen = connection.execute("SELECT payload_sha256 FROM sales_events WHERE event_id=?",
                                  (event_id,)).fetchone()
        if seen:
            if seen[0] != payload_hash:
                raise SalesVerificationError("A reused PayPal event id has different content.")
            connection.rollback()
            return {"event_id": event_id, "duplicate": True}
        connection.execute("INSERT INTO sales_events VALUES(?,?,?,?)",
                           (event_id, event_type, payload_hash, time.time()))
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            _, order_id = _capture_ids(resource)
            result = _record_completed(event, resource, order_loader(order_id), connection)
        else:
            row = _order_for_event(resource, connection)
            if row is None:
                result = {"event_id": event_id, "ignored": True,
                          "reason": "no matching FrontDesk sale"}
            elif event_type in {"PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"}:
                status = "refunded" if event_type.endswith("REFUNDED") else "reversed"
                connection.execute(
                    "UPDATE sales_orders SET status=?,refund_status=?,updated_at=? WHERE order_id=?",
                    (status, status, time.time(), row["order_id"]),
                )
                connection.execute(
                    "UPDATE sales_entitlements SET status='revoked',revoked_at=? WHERE order_id=?",
                    (time.time(), row["order_id"]),
                )
                _audit_action(connection, row["order_id"], "paypal", status, event_id)
                result = {"order_id": row["order_id"], "status": status,
                          "entitlement": "revoked"}
            elif event_type.startswith("CUSTOMER.DISPUTE."):
                state = event_type.rsplit(".", 1)[-1].lower()
                connection.execute(
                    "UPDATE sales_orders SET dispute_status=?,updated_at=? WHERE order_id=?",
                    (state, time.time(), row["order_id"]),
                )
                connection.execute(
                    "UPDATE sales_entitlements SET status='suspended' WHERE order_id=?",
                    (row["order_id"],),
                )
                _audit_action(connection, row["order_id"], "paypal", "dispute", state)
                result = {"order_id": row["order_id"], "dispute_status": state,
                          "entitlement": "suspended", "manual_review": True}
            elif event_type == "PAYMENT.REFUND.FAILED":
                connection.execute(
                    "UPDATE sales_orders SET refund_status='failed',updated_at=? WHERE order_id=?",
                    (time.time(), row["order_id"]),
                )
                _audit_action(connection, row["order_id"], "paypal", "refund_failed", event_id)
                result = {"order_id": row["order_id"], "refund_status": "failed",
                          "manual_review": True}
            else:
                result = {"event_id": event_id, "ignored": True,
                          "reason": "event type is informational"}
        connection.commit()
        return {"event_id": event_id, "duplicate": False, **result}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def approve_tax(order_id: str, *, actor: str, jurisdiction: str,
                note: str = "", database: Path | None = None) -> dict:
    if not actor.strip() or not jurisdiction.strip():
        raise SalesError("Tax approval requires an actor and jurisdiction.")
    with connect(database) as connection:
        row = connection.execute("SELECT status FROM sales_orders WHERE order_id=?",
                                 (order_id,)).fetchone()
        if row is None or row["status"] != "paid":
            raise SalesError("A paid order is required before tax approval.")
        now = time.time()
        connection.execute(
            "UPDATE sales_orders SET tax_status='approved',updated_at=? WHERE order_id=?",
            (now, order_id),
        )
        connection.execute(
            "UPDATE sales_entitlements SET status='active',issued_at=COALESCE(issued_at,?),"
            "revoked_at=NULL WHERE order_id=?",
            (now, order_id),
        )
        _audit_action(connection, order_id, actor, "tax_approved",
                      f"jurisdiction={jurisdiction}; {note}".strip())
    return {"order_id": order_id, "tax_status": "approved", "entitlement": "active"}


def _signing_secret(secret: str | None = None) -> bytes:
    value = secret if secret is not None else os.environ.get("SHELLIE_SALES_SIGNING_SECRET", "")
    if len(value.encode()) < 32:
        raise SalesConfigurationError("SHELLIE_SALES_SIGNING_SECRET must be at least 32 bytes.")
    return value.encode()


def _signed_value(kind: str, order_id: str, capture_id: str, issued_at: int,
                  secret: str | None = None) -> str:
    payload = f"{TOKEN_VERSION}|{kind}|{order_id}|{capture_id}|{issued_at}"
    signature = hmac.new(_signing_secret(secret), payload.encode(), hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    if kind == "download":
        return encoded_payload + "." + encoded_signature
    compact = encoded_signature[:24].upper()
    return f"{TOKEN_VERSION}-{compact[:8]}-{compact[8:16]}-{compact[16:24]}"


def claim(order_id: str, buyer_email: str, *, secret: str | None = None,
          database: Path | None = None) -> dict:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT o.*,e.status AS entitlement_status,e.issued_at FROM sales_orders o "
            "JOIN sales_entitlements e ON e.order_id=o.order_id WHERE o.order_id=?",
            (order_id.strip(),),
        ).fetchone()
        supplied = buyer_email.strip().casefold()
        if row is None or not supplied or not hmac.compare_digest(row["buyer_email"], supplied):
            raise SalesClaimError("Purchase could not be verified.")
        if row["entitlement_status"] != "active":
            raise SalesClaimError("Purchase is not eligible for fulfilment; contact support.")
        issued_at = int(row["issued_at"] or time.time())
        connection.execute(
            "UPDATE sales_entitlements SET claim_count=claim_count+1,last_claimed_at=? WHERE order_id=?",
            (time.time(), row["order_id"]),
        )
        _audit_action(connection, row["order_id"], "buyer", "entitlement_claimed", "")
    return {
        "order_id": row["order_id"], "receipt_id": row["receipt_id"],
        "license_key": _signed_value("license", row["order_id"], row["capture_id"], issued_at, secret),
        "download_token": _signed_value("download", row["order_id"], row["capture_id"], issued_at, secret),
    }


def verify_download_token(token: str, *, secret: str | None = None,
                          database: Path | None = None,
                          max_age_days: int = 30) -> dict:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(
            (encoded_payload + "=" * (-len(encoded_payload) % 4)).encode())
        signature = base64.urlsafe_b64decode(
            (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode())
        canonical_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        canonical_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        if (not hmac.compare_digest(encoded_payload, canonical_payload) or
                not hmac.compare_digest(encoded_signature, canonical_signature)):
            raise ValueError
        expected = hmac.new(_signing_secret(secret), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        version, kind, order_id, capture_id, issued_text = payload.decode().split("|", 4)
        issued_at = int(issued_text)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        raise SalesClaimError("Invalid download token.") from None
    if version != TOKEN_VERSION or kind != "download":
        raise SalesClaimError("Invalid download token.")
    if time.time() > issued_at + max_age_days * 86400:
        raise SalesClaimError("Download token has expired.")
    with connect(database) as connection:
        row = connection.execute(
            "SELECT o.order_id,o.capture_id,o.receipt_id,e.status FROM sales_orders o "
            "JOIN sales_entitlements e ON e.order_id=o.order_id WHERE o.order_id=?",
            (order_id,),
        ).fetchone()
    if row is None or row["capture_id"] != capture_id or row["status"] != "active":
        raise SalesClaimError("Download entitlement is not active.")
    return dict(row)


def receipt(order_id: str, *, database: Path | None = None) -> dict:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT receipt_id,order_id,plan_id,status,amount,currency,tax_amount,tax_currency,"
            "tax_status,paid_at FROM sales_orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        raise SalesError("Order not found.")
    return dict(row)


def refund(capture_id: str, *, amount: str | None = None,
           currency: str = "USD", reason: str = "Customer request") -> dict:
    payload: dict[str, object] = {"note_to_payer": reason[:255]}
    if amount is not None:
        payload["amount"] = {"value": _money(amount), "currency_code": currency.upper()}
    key = "shellie-refund-" + hashlib.sha256(
        f"{capture_id}|{amount or 'full'}".encode()).hexdigest()[:24]
    return _authed("POST", f"/v2/payments/captures/{capture_id}/refund", payload, key)


def status(database: Path | None = None) -> dict:
    with connect(database) as connection:
        orders = connection.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0]
        active = connection.execute(
            "SELECT COUNT(*) FROM sales_entitlements WHERE status='active'").fetchone()[0]
        holds = connection.execute(
            "SELECT COUNT(*) FROM sales_entitlements WHERE status='held_tax'").fetchone()[0]
    return {"database": str(database or database_path()), "schema_version": SCHEMA_VERSION,
            "orders": orders, "active_entitlements": active, "tax_holds": holds}
