"""PayPal Orders API v2 client, standard library only.

The flow is PayPal's own three steps:

    1. create_order   - create the order and get an approval URL; no money moves
    2. (the buyer approves on PayPal's hosted page; card details never reach us)
    3. capture_order  - settle the approved order; this is where money moves

Refunds go through refund_capture. Capture and refund are hard to undo, so
tools.py marks them dangerous=True and puts them behind the confirmation gate.

Credentials come from the environment:

    PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET
    PAYPAL_ENV = sandbox (default) | live

Live is used only when it is set explicitly. Anything else falls back to the
safe side, sandbox.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

import regions

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"

# Derived from the configured regions, so adding a market cannot forget this.
SUPPORTED_CURRENCIES = regions.SUPPORTED_CURRENCIES


class PayPalError(RuntimeError):
    """A PayPal API failure. The tool layer returns it to the model as is_error."""


class PayPalNotConfigured(PayPalError):
    """No credentials configured."""


# Token cache; reused until expires_in runs out.
_token_cache: dict = {"token": None, "expires_at": 0.0}


def base_url() -> str:
    return LIVE_BASE if os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live" else SANDBOX_BASE


def is_configured() -> bool:
    return bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))


def _ssl_context() -> ssl.SSLContext:
    """TLS context for PayPal. Even in compatibility mode, CA and hostname verification stay on."""
    context = ssl.create_default_context()
    if os.environ.get("PAYPAL_TLS_COMPAT", "").strip() == "1":
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    else:
        # Python/OpenSSL builds differ on whether STRICT is part of the default
        # flags. Make the production policy explicit instead of inheriting that
        # platform difference.
        context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


def _request(method: str, path: str, headers: dict[str, str], body: bytes | None) -> dict:
    """One HTTP round trip. Tests replace this function."""
    request = urllib.request.Request(
        base_url() + path, data=body, headers=headers, method=method
    )
    try:
        # base_url returns one of the two module-fixed PayPal HTTPS origins.
        with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as response:  # nosec B310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("message") or parsed.get("error_description") or detail
            issues = "; ".join(
                issue.get("description", "")
                for issue in parsed.get("details", [])
                if issue.get("description")
            )
            if issues:
                message = f"{message} ({issues})"
        except json.JSONDecodeError:
            message = detail[:300]
        raise PayPalError(f"PayPal API error {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise PayPalError(f"Could not reach PayPal: {exc.reason}") from None
    return json.loads(raw) if raw else {}


def _access_token() -> str:
    if not is_configured():
        raise PayPalNotConfigured(
            "PayPal is not configured. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET "
            "(and PAYPAL_ENV=live for production; the default is sandbox)."
        )
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    credentials = base64.b64encode(
        f"{os.environ['PAYPAL_CLIENT_ID']}:{os.environ['PAYPAL_CLIENT_SECRET']}".encode()
    ).decode()
    data = _request(
        "POST",
        "/v1/oauth2/token",
        {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        b"grant_type=client_credentials",
    )
    token = data.get("access_token")
    if not token:
        raise PayPalError("PayPal did not return an access token.")
    # Treat the token as expired 60 seconds early, for margin.
    _token_cache.update(token=token, expires_at=now + int(data.get("expires_in", 0)) - 60)
    return token


def _authed(method: str, path: str, payload: dict | None = None,
            idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        # A retry with the same key returns the same result, which is what stops a
# double charge or a double refund.
        headers["PayPal-Request-Id"] = idempotency_key
    body = json.dumps(payload).encode() if payload is not None else None
    return _request(method, path, headers, body)


def _validate_amount(value: str, currency: str) -> str:
    if currency not in SUPPORTED_CURRENCIES:
        raise PayPalError(
            f"Unsupported currency: {currency} "
            f"(supported: {', '.join(SUPPORTED_CURRENCIES)})")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise PayPalError(f"Invalid amount: {value!r}") from None
    if not amount.is_finite():
        raise PayPalError(f"Invalid amount: {value!r}")
    if amount.normalize().as_tuple().exponent < -2:
        raise PayPalError("Amount must have no more than two decimal places.")
    if amount <= 0:
        raise PayPalError("Amount must be greater than zero.")
    if amount > Decimal("10000"):
        # A ceiling to limit the damage of a mistake. Tune it to the deployment.
        raise PayPalError("Amount exceeds the $10,000 per-transaction limit.")
    return format(amount.quantize(Decimal("0.01")), "f")


def _operation_key(prefix: str, operation_id: str) -> str:
    operation_id = operation_id.strip()
    if not operation_id:
        raise PayPalError("A stable operation_id is required for this payment operation.")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:28]
    return f"{prefix}-{digest}"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def create_order(amount: str, currency: str = "", description: str = "",
                 reference_id: str | None = None) -> dict:
    """Create an order and return the buyer's approval URL. No money moves yet."""
    currency = (currency or regions.currency()).upper()
    value = _validate_amount(amount, currency)
    unit: dict = {"amount": {"currency_code": currency, "value": value}}
    if description:
        unit["description"] = description[:127]
    if reference_id:
        unit["reference_id"] = reference_id
    data = _authed(
        "POST", "/v2/checkout/orders",
        {"intent": "CAPTURE", "purchase_units": [unit]},
        idempotency_key=reference_id,
    )
    approval = next(
        (link["href"] for link in data.get("links", [])
         if link.get("rel") in ("approve", "payer-action")),
        None,
    )
    checkout_approval = None
    if data.get("id"):
        # Use the signed SDK v6 page only when it is configured explicitly. Otherwise
        # return PayPal's own hosted approval URL and leave existing setups alone.
        from paypal_checkout import CheckoutError, create_checkout_url
        try:
            checkout_approval = create_checkout_url(data["id"], value, currency)
        except CheckoutError as exc:
            raise PayPalError(str(exc)) from None
    return {
        "order_id": data.get("id"),
        "status": data.get("status"),
        "amount": value,
        "currency": currency,
        "approval_url": checkout_approval or approval,
        "environment": os.environ.get("PAYPAL_ENV", "sandbox"),
    }


def get_order(order_id: str) -> dict:
    """Return the order's current state; used to check whether it was approved."""
    data = _authed("GET", f"/v2/checkout/orders/{order_id}")
    return {
        "order_id": data.get("id"),
        "status": data.get("status"),  # CREATED / APPROVED / COMPLETED / VOIDED
        "purchase_units": [
            {
                "amount": unit.get("amount"),
                "captures": [
                    {"capture_id": cap.get("id"), "status": cap.get("status")}
                    for cap in (unit.get("payments") or {}).get("captures", [])
                ],
            }
            for unit in data.get("purchase_units", [])
        ],
    }


def capture_order(order_id: str) -> dict:
    """Settle an approved order. This is where money moves."""
    data = _authed("POST", f"/v2/checkout/orders/{order_id}/capture", {},
                   idempotency_key=f"cap-{order_id}")
    captures = [
        {
            "capture_id": cap.get("id"),
            "status": cap.get("status"),
            "amount": cap.get("amount"),
        }
        for unit in data.get("purchase_units", [])
        for cap in (unit.get("payments") or {}).get("captures", [])
    ]
    return {"order_id": data.get("id"), "status": data.get("status"), "captures": captures}


def refund_capture(capture_id: str, amount: str | None = None,
                   currency: str = "", note: str = "",
                   operation_id: str = "") -> dict:
    """Refund a settled payment. Omitting amount refunds the full amount."""
    currency = (currency or regions.currency()).upper()
    payload: dict = {}
    if amount is not None:
        payload["amount"] = {
            "currency_code": currency,
            "value": _validate_amount(amount, currency),
        }
    if note:
        payload["note_to_payer"] = note[:255]
    data = _authed("POST", f"/v2/payments/captures/{capture_id}/refund",
                   payload or None, idempotency_key=_operation_key("ref", operation_id))
    return {
        "refund_id": data.get("id"),
        "status": data.get("status"),
        "amount": data.get("amount"),
    }
