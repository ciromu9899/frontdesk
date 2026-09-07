"""Seller-side, sandbox-only Paddle checkout and delivery. No customer app imports."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class CommerceError(ValueError):
    pass


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CommerceError("Timezone required")
    return parsed.timestamp()


def identifier(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[a-z0-9]{26}", value):
        raise CommerceError("Invalid identifier")
    return value


def verify_signature(raw: bytes, header: str, secret: str, now: float) -> None:
    if not secret or len(raw) > 1_000_000:
        raise CommerceError("Invalid webhook")
    parts = [part.strip().split("=", 1) for part in header.split(";")]
    stamps = [value for key, value in parts if key == "ts"] if all(len(p) == 2 for p in parts) else []
    if len(stamps) != 1 or not stamps[0].isdigit() or abs(now - int(stamps[0])) > 5:
        raise CommerceError("Invalid webhook timestamp")
    expected = hmac.new(secret.encode(), stamps[0].encode() + b":" + raw, hashlib.sha256).hexdigest()
    if not any(key == "h1" and hmac.compare_digest(expected, value) for key, value in parts):
        raise CommerceError("Invalid webhook signature")


class SandboxAPI:
    def __init__(self, key: str):
        if not key.startswith("pdl_sdbx_apikey_"):
            raise CommerceError("Only sandbox API keys are accepted")
        self.key = key

    def __call__(self, method: str, path: str, payload: dict | None = None) -> dict:
        if path != "/transactions" or method != "POST":
            raise CommerceError("API operation not permitted")
        request = urllib.request.Request(
            "https://sandbox-api.paddle.com" + path,
            data=json.dumps(payload).encode(), method=method,
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json", "Paddle-Version": "1"},
        )
        # No retries: ambiguous transaction creation must be reconciled, not repeated.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise CommerceError("API redirect rejected")
        with urllib.request.build_opener(NoRedirect).open(request, timeout=4) as response:
            return json.loads(response.read(1_000_000))["data"]


class Store:
    def __init__(self, database: Path, price_id: str, api, clock=time.time):
        self.database = database
        self.price_id = identifier(price_id, "pri")
        self.api = api
        self.clock = clock
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS checkout (
                    token_hash TEXT PRIMARY KEY, expires REAL NOT NULL,
                    transaction_id TEXT UNIQUE, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resources (
                    id TEXT PRIMARY KEY, occurred REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS holds (transaction_id TEXT PRIMARY KEY);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("INSERT INTO checkout VALUES (?, ?, NULL, 'new')", (self.token_hash(token), self.clock() + 86400))
        return token

    @staticmethod
    def token_hash(token: str) -> str:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise CommerceError("Invalid session")
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, db, token):
        row = db.execute("SELECT * FROM checkout WHERE token_hash=?", (self.token_hash(token),)).fetchone()
        if row is None or row["expires"] <= self.clock():
            raise CommerceError("Session expired or invalid")
        return row

    def checkout(self, token: str) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.session(db, token)
            if row["transaction_id"]:
                return row["transaction_id"]
            if row["state"] != "new":
                raise CommerceError("Transaction creation uncertain; contact support, do not pay again")
            db.execute("UPDATE checkout SET state='creating' WHERE token_hash=?", (row["token_hash"],))
        transaction = self.api("POST", "/transactions", {
            "items": [{"price_id": self.price_id, "quantity": 1}], "collection_mode": "automatic",
        })
        transaction_id = identifier(transaction["id"], "txn")
        with self.connection() as db:
            db.execute("UPDATE checkout SET transaction_id=?, state='ready' WHERE token_hash=?", (transaction_id, self.token_hash(token)))
        return transaction_id

    def webhook(self, raw: bytes, signature: str, secret: str) -> dict:
        verify_signature(raw, signature, secret, self.clock())
        event = json.loads(raw)
        event_id = identifier(event["event_id"], "evt")
        occurred = timestamp(event["occurred_at"])
        kind = event["event_type"]
        data = event["data"]
        digest = hashlib.sha256(raw).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT digest FROM events WHERE id=?", (event_id,)).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise CommerceError("Event identifier collision")
                return {"duplicate": True}
            if kind.startswith(("transaction.", "subscription.")):
                prefix = "txn" if kind.startswith("transaction.") else "sub"
                resource_id = identifier(data["id"], prefix)
                # Do not retain addresses, payment details, or full webhook payloads.
                reduced = {key: data.get(key) for key in (
                    "id", "status", "customer_id", "subscription_id", "current_billing_period", "scheduled_change")}
                reduced["prices"] = [item.get("price", {}).get("id") for item in data.get("items", [])]
                reduced["quantities"] = [item.get("quantity") for item in data.get("items", [])]
                reduced["completed"] = kind == "transaction.completed"
                old = db.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
                if old is None or occurred > old["occurred"]:
                    # A completed transaction remains evidence of payment after later metadata events.
                    if old and prefix == "txn":
                        reduced["completed"] |= json.loads(old["payload"]).get("completed", False)
                    db.execute("INSERT OR REPLACE INTO resources VALUES (?, ?, ?)", (resource_id, occurred, json.dumps(reduced)))
                elif occurred == old["occurred"] and json.loads(old["payload"]) != reduced:
                    raise CommerceError("Ambiguous resource ordering; reconciliation required")
            elif kind.startswith("adjustment.") and data.get("status") == "approved" and data.get("action") in {"refund", "chargeback", "chargeback_warning"}:
                db.execute("INSERT OR IGNORE INTO holds VALUES (?)", (identifier(data["transaction_id"], "txn"),))
            db.execute("INSERT INTO events VALUES (?, ?)", (event_id, digest))
        return {"received": True}

    def status(self, token: str) -> dict:
        with self.connection() as db:
            row = self.session(db, token)
            def resource(resource_id):
                found = db.execute("SELECT payload FROM resources WHERE id=?", (resource_id,)).fetchone()
                return json.loads(found["payload"]) if found else {}
            transaction = resource(row["transaction_id"])
            subscription = resource(transaction.get("subscription_id"))
            held = db.execute("SELECT 1 FROM holds WHERE transaction_id=?", (row["transaction_id"],)).fetchone()
        period_end = (subscription.get("current_billing_period") or {}).get("ends_at")
        change = subscription.get("scheduled_change") or {}
        deadline = timestamp(period_end) if period_end else 0
        if change.get("action") in {"cancel", "pause"} and change.get("effective_at"):
            deadline = min(deadline, timestamp(change["effective_at"]))
        allowed = bool(
            transaction.get("completed") and transaction.get("status") == "completed"
            and transaction.get("prices") == [self.price_id] and transaction.get("quantities") == [1]
            and subscription.get("prices") == [self.price_id] and subscription.get("quantities") == [1]
            and transaction.get("customer_id") and transaction.get("customer_id") == subscription.get("customer_id")
            and subscription.get("status") in {"active", "trialing"} and deadline > self.clock() and not held
        )
        return {"download_allowed": allowed, "status": subscription.get("status", "awaiting_payment"),
                "scheduled_change": change or None, "hold": bool(held)}

    def download(self, token: str, package: Path, expected_hash: str) -> bytes:
        if not self.status(token)["download_allowed"]:
            raise CommerceError("Delivery not authorized")
        if package.suffix != ".zip" or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise CommerceError("Package not configured")
        content = package.read_bytes()
        if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_hash):
            raise CommerceError("Package integrity failure")
        return content
