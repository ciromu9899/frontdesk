import hashlib
import hmac
import json
import tempfile
import unittest
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from paddle_sales import CommerceError, SandboxAPI, Store, verify_signature
from paddle_sales_server import handler


def ident(prefix, digit="1"):
    return prefix + "_" + digit * 26


class PaddleSalesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sandbox.db"
        self.now = 1800000000
        self.api = Mock(return_value={"id": ident("txn")})
        self.store = Store(self.path, ident("pri"), self.api, lambda: self.now)
        self.token = self.store.start()
        self.secret = "test-only-webhook-secret"
        self.counter = 0

    def event(self, kind, data, occurred=None):
        self.counter += 1
        event = {"event_id": "evt_" + str(self.counter).zfill(26), "event_type": kind,
                 "occurred_at": datetime.fromtimestamp(occurred or self.now, timezone.utc).isoformat(), "data": data}
        raw = json.dumps(event).encode()
        signature = hmac.new(self.secret.encode(), str(self.now).encode() + b":" + raw, hashlib.sha256).hexdigest()
        return raw, f"ts={self.now};h1={signature}"

    def send(self, kind, data, occurred=None):
        raw, sig = self.event(kind, data, occurred)
        return self.store.webhook(raw, sig, self.secret)

    def subscription(self, status="active", change=None, price=None):
        return {"id": ident("sub"), "customer_id": ident("ctm"), "status": status,
                "items": [{"price": {"id": price or ident("pri")}, "quantity": 1}],
                "current_billing_period": {"ends_at": datetime.fromtimestamp(self.now+3600, timezone.utc).isoformat()},
                "scheduled_change": change}

    def transaction(self):
        return {"id": ident("txn"), "subscription_id": ident("sub"), "customer_id": ident("ctm"),
                "status": "completed", "items": [{"price": {"id": ident("pri")}, "quantity": 1}]}

    def purchase(self):
        self.store.checkout(self.token)
        self.send("subscription.created", self.subscription())
        self.send("transaction.completed", self.transaction())

    def test_purchase_and_delivery(self):
        self.assertFalse(self.store.status(self.token)["download_allowed"])
        self.purchase()
        self.assertTrue(self.store.status(self.token)["download_allowed"])
        package = Path(self.temp.name) / "test.zip"
        package.write_bytes(b"test fixture, not a distribution archive")
        expected = hashlib.sha256(package.read_bytes()).hexdigest()
        self.assertEqual(self.store.download(self.token, package, expected), package.read_bytes())
        with self.assertRaises(CommerceError):
            self.store.download(self.token, package, "0" * 64)

    def test_checkout_retry_does_not_create_second_transaction(self):
        self.store.checkout(self.token)
        self.store.checkout(self.token)
        self.api.assert_called_once()

    def test_ambiguous_api_failure_is_not_retried(self):
        self.api.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.store.checkout(self.token)
        with self.assertRaises(CommerceError):
            self.store.checkout(self.token)
        self.api.assert_called_once()

    def test_duplicate_survives_new_store(self):
        raw, sig = self.event("subscription.created", self.subscription())
        self.store.webhook(raw, sig, self.secret)
        restarted = Store(self.path, ident("pri"), self.api, lambda: self.now)
        self.assertTrue(restarted.webhook(raw, sig, self.secret)["duplicate"])

    def test_tamper_and_expired_signatures(self):
        raw, sig = self.event("subscription.created", self.subscription())
        for body, signature, now in [(raw+b" ", sig, self.now), (raw, sig, self.now+6), (raw, "broken", self.now)]:
            with self.subTest(signature=signature[:12]), self.assertRaises(CommerceError):
                verify_signature(body, signature, self.secret, now)

    def test_scheduled_cancel_then_expiry(self):
        self.purchase()
        effective = datetime.fromtimestamp(self.now+100, timezone.utc).isoformat()
        self.send("subscription.updated", self.subscription(change={"action": "cancel", "effective_at": effective}), self.now+1)
        self.assertTrue(self.store.status(self.token)["download_allowed"])
        self.now += 101
        self.assertFalse(self.store.status(self.token)["download_allowed"])

    def test_cancel_and_old_active_event(self):
        self.purchase()
        self.send("subscription.canceled", self.subscription("canceled"), self.now+2)
        self.send("subscription.updated", self.subscription(), self.now+1)
        self.assertFalse(self.store.status(self.token)["download_allowed"])

    def test_failed_payment_never_grants_delivery(self):
        self.store.checkout(self.token)
        self.send("subscription.created", self.subscription())
        data = self.transaction()
        data["status"] = "past_due"
        self.send("transaction.payment_failed", data)
        self.assertFalse(self.store.status(self.token)["download_allowed"])

    def test_wrong_price_and_customer(self):
        self.purchase()
        self.send("subscription.updated", self.subscription(price=ident("pri", "2")), self.now+1)
        self.assertFalse(self.store.status(self.token)["download_allowed"])
        data = self.subscription()
        data["customer_id"] = ident("ctm", "2")
        self.send("subscription.updated", data, self.now+2)
        self.assertFalse(self.store.status(self.token)["download_allowed"])

    def test_another_session_cannot_claim_purchase(self):
        self.purchase()
        other = self.store.start()
        self.assertFalse(self.store.status(other)["download_allowed"])
        with self.assertRaises(CommerceError):
            self.store.status("not-a-token")

    def test_refund_hold(self):
        self.purchase()
        self.send("adjustment.updated", {"status": "approved", "action": "refund", "transaction_id": ident("txn")})
        self.assertFalse(self.store.status(self.token)["download_allowed"])

    def test_expired_session(self):
        self.now += 86401
        with self.assertRaises(CommerceError):
            self.store.status(self.token)

    def test_production_key_rejected(self):
        with self.assertRaises(CommerceError):
            SandboxAPI("pdl_live_apikey_never-use")

    def test_past_due_and_recovery(self):
        self.purchase()
        self.send("subscription.past_due", self.subscription("past_due"), self.now+1)
        self.assertFalse(self.store.status(self.token)["download_allowed"])
        self.send("subscription.activated", self.subscription(), self.now+2)
        self.assertTrue(self.store.status(self.token)["download_allowed"])

    def test_http_purchase_download_and_cancel(self):
        package = Path(self.temp.name) / "http.zip"
        package.write_bytes(b"sandbox download fixture")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(
            self.store, self.secret, "test_public_fixture",
            "https://sandbox-customer-portal.paddle.com/"+ident("cpl"),
            package, hashlib.sha256(package.read_bytes()).hexdigest(), "http://test-origin"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            def request(path, body=None, headers=None):
                req = urllib.request.Request(base+path, data=body, headers=headers or {})
                return urllib.request.urlopen(req, timeout=3)
            auth = {"Authorization": "Bearer "+self.token}
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request("/download", headers=auth)
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError):
                request("/checkout", b"", auth)
            with request("/checkout", b"", {**auth, "Origin": "http://test-origin", "X-FrontDesk-Request": "sandbox"}) as response:
                self.assertEqual(json.load(response)["transaction_id"], ident("txn"))
            for kind, data in [("subscription.created", self.subscription()), ("transaction.completed", self.transaction())]:
                raw, sig = self.event(kind, data)
                with request("/webhook", raw, {"Paddle-Signature": sig}) as response:
                    self.assertEqual(response.status, 200)
            with request("/download", headers=auth) as response:
                self.assertEqual(response.read(), package.read_bytes())
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            raw, sig = self.event("subscription.canceled", self.subscription("canceled"), self.now+1)
            with request("/webhook", raw, {"Paddle-Signature": sig}) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request("/download", headers=auth)
            self.assertEqual(denied.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
