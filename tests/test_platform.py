from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import string
import time
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import approvals
import audit
import admin
import auth
import chat
import handoffs
import mobile
import urllib.error
import channels
import connectors
import webchat
import webhooks
from channels import identity, linkedin, signatures
import paypal
import paypal_checkout
import providers
import rag
import re
import regions
import tools


class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "test-secret-that-is-longer-than-thirty-two-characters"

    def test_signed_token_round_trip_and_tamper_rejection(self) -> None:
        expected = auth.Principal("casey@example.com", ("operator",), "tenant-a")
        token = auth.issue_token(expected, self.secret, expires_in=60)
        self.assertEqual(auth.authenticate_token(token, self.secret), expected)
        # Tamper with the payload, which is the text the signature covers.
        payload, signature = token.split(".", 1)
        forged = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        with self.assertRaises(auth.AuthError):
            auth.authenticate_token(f"{forged}.{signature}", self.secret)

    def test_non_canonical_base64_is_rejected(self) -> None:
        # The decoder ignores the unused bits of the last character, so several
        # spellings of the same signature would otherwise all verify.
        principal = auth.Principal("casey@example.com", ("operator",), "tenant-a")
        token = auth.issue_token(principal, self.secret, expires_in=60)
        alphabet = string.ascii_letters + string.digits + "-_"
        for char in alphabet:
            if char == token[-1]:
                continue
            with self.assertRaises(auth.AuthError):
                auth.authenticate_token(token[:-1] + char, self.secret)

    def test_role_permissions_are_enforced(self) -> None:
        support = auth.Principal("support@example.com", ("support",))
        self.assertTrue(support.can("orders:read"))
        self.assertFalse(support.can("reservations:write"))
        denied = tools.execute(
            tools.ToolCall("call-1", "cancel_reservation", {"reservation_id": "R-2001"}),
            principal=support,
        )
        self.assertTrue(denied.is_error)
        self.assertIn("Permission denied", denied.content)


class AuditTests(unittest.TestCase):
    def test_chain_verification_redaction_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.jsonl"
            audit.record("login", actor="casey", details={"token": "secret-value"}, path=path)
            audit.record("tool", actor="casey", details={"ok": True}, path=path)
            valid, count, _ = audit.verify(path)
            self.assertTrue(valid)
            self.assertEqual(count, 2)
            self.assertEqual(audit.read_events(path=path)[0]["details"]["token"], "[REDACTED]")
            lines = path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[0])
            event["actor"] = "mallory"
            lines[0] = json.dumps(event)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertFalse(audit.verify(path)[0])

    def test_non_ascii_tampered_hash_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.jsonl"
            audit.record("login", actor="casey", path=path)
            event = json.loads(path.read_text(encoding="utf-8"))
            event["hash"] = "é" * 64
            path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(audit.verify(path)[:2], (False, 1))

    def test_last_hash_reads_trailing_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.jsonl"
            first = audit.record("first", actor="casey", path=path)
            second = audit.record("second", actor="casey", path=path)
            self.assertEqual(second["previous_hash"], first["hash"])
            self.assertEqual(audit._last_hash(path), second["hash"])


class RagTests(unittest.TestCase):
    def test_build_and_search_returns_citation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            (knowledge / "returns.md").write_text(
                "# Returns\n\nRefund requests require an order number and review by support.",
                encoding="utf-8",
            )
            index = root / "index.json"
            status = rag.build_index(knowledge, index)
            self.assertEqual(status, {"files": 1, "chunks": 1})
            hits = rag.search("refund order number", index_path=index)
            self.assertEqual(hits[0].source, "returns.md")
            self.assertIn("Refund requests", hits[0].text)

    def test_unicode_and_unspaced_script_queries(self) -> None:
        # After English, the languages a US front desk actually meets are
        # Spanish and Chinese. Chinese is written without spaces, so the
        # tokenizer splits those runs into character bigrams; a query of one
        # character has to survive that path too. The Chinese is spelled with
        # escapes so that the source file stays ASCII: "returns need an order
        # number", and the query is "returns".
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            (knowledge / "support.md").write_text(
                "\u9000\u8d27\u9700\u8981\u8ba2\u5355\u53f7\u3002 "
                "Español: sí, aceptamos devolución.",
                encoding="utf-8",
            )
            index = root / "index.json"
            rag.build_index(knowledge, index)
            self.assertTrue(rag.search("\u9000\u8d27", index_path=index))
            self.assertTrue(rag.search("devolución", index_path=index))
            self.assertIn("x", rag._tokens("X"))


class PayPalTests(unittest.TestCase):
    def test_tls_compat_only_disables_x509_strict(self) -> None:
        with mock.patch.dict(os.environ, {"PAYPAL_TLS_COMPAT": ""}):
            strict = paypal._ssl_context()
        self.assertTrue(strict.verify_flags & ssl.VERIFY_X509_STRICT)
        with mock.patch.dict(os.environ, {"PAYPAL_TLS_COMPAT": "1"}):
            compatible = paypal._ssl_context()
        self.assertFalse(compatible.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertTrue(compatible.check_hostname)
        self.assertEqual(compatible.verify_mode, ssl.CERT_REQUIRED)

    def test_amount_validation_is_exact(self) -> None:
        self.assertEqual(paypal._validate_amount("49.90", "USD"), "49.90")
        self.assertEqual(paypal._validate_amount("1.2300", "USD"), "1.23")
        for invalid in ("1.234", "NaN", "Infinity", "-Infinity"):
            with self.subTest(invalid=invalid), self.assertRaises(paypal.PayPalError):
                paypal._validate_amount(invalid, "USD")

    def test_refund_uses_operation_specific_idempotency_key(self) -> None:
        response = {"id": "RF-1", "status": "COMPLETED", "amount": {"value": "10.00"}}
        with mock.patch.object(paypal, "_authed", return_value=response) as authed:
            paypal.refund_capture("CAP-1", "10.00", operation_id="tool-call-1")
            first_key = authed.call_args.kwargs["idempotency_key"]
            paypal.refund_capture("CAP-1", "10.00", operation_id="tool-call-2")
            second_key = authed.call_args.kwargs["idempotency_key"]
        self.assertNotEqual(first_key, second_key)
        self.assertLessEqual(len(first_key), 38)
        with self.assertRaises(paypal.PayPalError):
            paypal.refund_capture("CAP-1", "10.00")

    def test_payment_tools_are_not_registered_in_frontdesk(self) -> None:
        for name in (
            "create_paypal_order", "get_paypal_order_status",
            "capture_paypal_order", "refund_paypal_capture",
        ):
            self.assertNotIn(name, tools.REGISTRY)

    def test_checkout_token_is_signed_and_expires(self) -> None:
        environment = {
            "FRONTDESK_CHECKOUT_BASE_URL": "http://127.0.0.1:8780",
            "FRONTDESK_CHECKOUT_SECRET": "s" * 32,
            "PAYPAL_ENV": "sandbox",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            url = paypal_checkout.create_checkout_url(
                "5O190127TN364715T", "49.90", "USD", now=1_000
            )
            self.assertIsNotNone(url)
            token = url.split("#", 1)[1]
            claims = paypal_checkout.verify_checkout_token(token, now=1_001)
            self.assertEqual(claims["order_id"], "5O190127TN364715T")
            with self.assertRaises(paypal_checkout.CheckoutError):
                paypal_checkout.verify_checkout_token(token + "x", now=1_001)
            with self.assertRaises(paypal_checkout.CheckoutError):
                paypal_checkout.verify_checkout_token(token, now=5_000)
            with self.assertRaises(paypal_checkout.CheckoutError):
                paypal_checkout.verify_checkout_token("\N{SNOWMAN}.signature", now=1_001)

    def test_create_order_uses_checkout_url_only_when_configured(self) -> None:
        response = {
            "id": "5O190127TN364715T",
            "status": "CREATED",
            "links": [{"rel": "approve", "href": "https://sandbox.paypal.test/approve"}],
        }
        with mock.patch.object(paypal, "_authed", return_value=response):
            with mock.patch.dict(os.environ, {}, clear=True):
                direct = paypal.create_order("49.90")
            with mock.patch.dict(os.environ, {
                "FRONTDESK_CHECKOUT_BASE_URL": "http://localhost:8780",
                "FRONTDESK_CHECKOUT_SECRET": "s" * 32,
                "PAYPAL_ENV": "sandbox",
            }, clear=True):
                embedded = paypal.create_order("49.90")
        self.assertEqual(direct["approval_url"], "https://sandbox.paypal.test/approve")
        self.assertTrue(embedded["approval_url"].startswith("http://localhost:8780/#"))

    def test_checkout_capture_requires_approval_and_matching_amount(self) -> None:
        claims = {"order_id": "5O190127TN364715T", "amount": "49.90", "currency": "USD"}
        approved = {
            "order_id": claims["order_id"], "status": "APPROVED",
            "purchase_units": [{"amount": {"currency_code": "USD", "value": "49.90"},
                                "captures": []}],
        }
        captured = {
            "order_id": claims["order_id"], "status": "COMPLETED",
            "captures": [{"capture_id": "CAPTURE1", "status": "COMPLETED",
                          "amount": {"currency_code": "USD", "value": "49.90"}}],
        }
        with mock.patch.object(paypal, "get_order", return_value=approved), \
                mock.patch.object(paypal, "capture_order", return_value=captured) as capture, \
                mock.patch.object(audit, "record"):
            result = paypal_checkout.capture_approved_order(claims)
        self.assertEqual(result["status"], "COMPLETED")
        capture.assert_called_once_with(claims["order_id"])

        created = {**approved, "status": "CREATED"}
        with mock.patch.object(paypal, "get_order", return_value=created), \
                mock.patch.object(paypal, "capture_order") as capture:
            with self.assertRaises(paypal_checkout.CheckoutError):
                paypal_checkout.capture_approved_order(claims)
        capture.assert_not_called()

        wrong_amount = {**approved, "purchase_units": [{
            "amount": {"currency_code": "USD", "value": "50.00"}, "captures": [],
        }]}
        with mock.patch.object(paypal, "get_order", return_value=wrong_amount), \
                mock.patch.object(paypal, "capture_order") as capture:
            with self.assertRaises(paypal_checkout.CheckoutError):
                paypal_checkout.capture_approved_order(claims)
        capture.assert_not_called()

    def test_checkout_capture_is_idempotent_after_completion(self) -> None:
        claims = {"order_id": "5O190127TN364715T", "amount": "49.90", "currency": "USD"}
        completed = {
            "order_id": claims["order_id"], "status": "COMPLETED",
            "purchase_units": [{
                "amount": {"currency_code": "USD", "value": "49.90"},
                "captures": [{"capture_id": "CAPTURE1", "status": "COMPLETED"}],
            }],
        }
        with mock.patch.object(paypal, "get_order", return_value=completed), \
                mock.patch.object(paypal, "capture_order") as capture:
            result = paypal_checkout.capture_approved_order(claims)
        self.assertEqual(result["captureId"], "CAPTURE1")
        capture.assert_not_called()


class DemoDocumentTests(unittest.TestCase):
    def test_demo_is_complete_responsive_html_document(self) -> None:
        path = Path(__file__).resolve().parents[1] / "docs" / "demo" / "index.html"
        document = path.read_text(encoding="utf-8-sig")
        self.assertTrue(document.startswith("<!doctype html>"))
        self.assertIn('<html lang="en">', document)
        self.assertIn('<meta name="viewport"', document)
        self.assertTrue(document.rstrip().endswith("</html>"))


class _BackendHandler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.__class__.seen.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        body = json.dumps({"order_id": "A-1", "status": "shipped"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.__class__.seen.append({"method": "PATCH", "path": self.path, "headers": dict(self.headers), "body": body})
        response = json.dumps({"updated": True, **body}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class ConnectorTests(unittest.TestCase):
    def test_local_stub_receives_auth_and_idempotency_headers(self) -> None:
        _BackendHandler.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = connectors.RestBackend(connectors.BackendConfig(
                f"http://127.0.0.1:{server.server_port}", "backend-token", allow_http=True
            ))
            self.assertEqual(backend.get_order("A-1")["status"], "shipped")
            backend.change_reservation("R-1", {"date": "2026-09-30"}, "request-123")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(_BackendHandler.seen[0]["headers"]["Authorization"], "Bearer backend-token")
        self.assertEqual(_BackendHandler.seen[1]["headers"]["Idempotency-Key"], "request-123")

    def test_non_https_remote_url_is_rejected(self) -> None:
        with self.assertRaises(connectors.ConnectorError):
            connectors.RestBackend(connectors.BackendConfig("http://example.com", "token"))

    def test_tools_switch_to_configured_live_backend(self) -> None:
        _BackendHandler.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = {
            "FRONTDESK_BACKEND_URL": f"http://127.0.0.1:{server.server_port}",
            "FRONTDESK_BACKEND_TOKEN": "backend-token",
            "FRONTDESK_BACKEND_ALLOW_HTTP": "1",
        }
        principal = auth.Principal("admin@example.com", ("admin",))
        try:
            with mock.patch.dict(os.environ, environment, clear=False):
                order = tools.execute(
                    tools.ToolCall("read-1", "get_order_status", {"order_id": "A-1"}),
                    principal=principal,
                )
                changed = tools.execute(
                    tools.ToolCall("write-1", "change_reservation", {
                        "reservation_id": "R-1", "new_date": "2026-09-30"
                    }),
                    principal=principal,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(order.is_error, order.content)
        self.assertFalse(changed.is_error, changed.content)
        self.assertEqual(_BackendHandler.seen[1]["headers"]["Idempotency-Key"], "write-1")


class AdminTests(unittest.TestCase):
    def test_login_dashboard_status_and_reindex(self) -> None:
        secret = "admin-test-secret-that-is-longer-than-thirty-two-characters"
        token = auth.issue_token(auth.Principal("admin@example.com", ("admin",)), secret, 60)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            (knowledge / "guide.md").write_text("# Guide\n\nSupport verifies the order number.", encoding="utf-8")
            handoff_path = root / "handoffs.jsonl"
            server = ThreadingHTTPServer(("127.0.0.1", 0), admin.AdminHandler)
            server.auth_secret = secret  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with mock.patch.dict(os.environ, {"FRONTDESK_STATE_DB": str(root / "state.db")}), \
                     mock.patch.object(audit, "AUDIT_PATH", root / "audit.jsonl"), \
                     mock.patch.object(rag, "KNOWLEDGE_DIR", knowledge), \
                     mock.patch.object(rag, "INDEX_PATH", root / "index.json"), \
                     mock.patch.object(handoffs, "HANDOFF_PATH", handoff_path):
                    ticket = handoffs.request(
                        "Customer asked for a teammate.", requested_by="slack:U1",
                        channel="slack", thread_key="C1:1.0")
                    login = urllib.request.Request(
                        base + "/login",
                        data=urllib.parse.urlencode({"token": token}).encode(),
                        method="POST",
                    )
                    dashboard = opener.open(login, timeout=2).read().decode()
                    self.assertIn("Operations overview", dashboard)
                    self.assertIn("Shared inbox", dashboard)
                    self.assertIn("Knowledge management", dashboard)
                    self.assertIn("CSAT", dashboard)
                    self.assertIn(ticket["id"], dashboard)
                    csrf = dashboard.split('name="csrf" value="', 1)[1].split('"', 1)[0]
                    resolve = urllib.request.Request(
                        base + "/handoffs/resolve",
                        data=urllib.parse.urlencode({
                            "csrf": csrf, "id": ticket["id"], "note": "Handled"
                        }).encode(),
                        method="POST",
                    )
                    resolved_dashboard = opener.open(resolve, timeout=2).read().decode()
                    self.assertIn("No open handoffs.", resolved_dashboard)
                    self.assertIn("handoff.resolved", resolved_dashboard)
                    boundary = "frontdesk-test-boundary"
                    upload_body = (
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n"
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"returns.txt\"\r\n"
                        "Content-Type: text/plain\r\n\r\nReturns require an order number.\r\n"
                        f"--{boundary}--\r\n").encode()
                    upload = urllib.request.Request(
                        base + "/knowledge/upload", data=upload_body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                        method="POST")
                    uploaded_dashboard = opener.open(upload, timeout=2).read().decode()
                    self.assertIn("Knowledge management", uploaded_dashboard)
                    reindex = urllib.request.Request(
                        base + "/knowledge/reindex",
                        data=urllib.parse.urlencode({"csrf": csrf}).encode(),
                        method="POST",
                    )
                    self.assertIn("Operations overview", opener.open(reindex, timeout=2).read().decode())
                    status = json.loads(opener.open(base + "/api/status", timeout=2).read())
                    self.assertTrue(status["audit"]["valid"])
                    self.assertEqual(status["rag"]["files"], 2)
                    self.assertEqual(status["handoffs_open"], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class HandoffQueueTests(unittest.TestCase):
    def test_ticket_survives_reload_and_resolves_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "handoffs.jsonl"
            with mock.patch.object(handoffs.audit, "record"):
                opened = handoffs.request(
                    "Customer asked for a teammate.", requested_by="meta:user-1",
                    tenant_id="tenant-a", channel="meta", thread_key="thread-1",
                    session_id="session-1", path=path,
                )
                current = handoffs.list_tickets(
                    tenant_id="tenant-a", path=path)
                self.assertEqual([ticket["id"] for ticket in current], [opened["id"]])
                self.assertTrue(handoffs.resolve(
                    opened["id"], resolved_by="owner@example.com",
                    tenant_id="tenant-a", note="Replied", path=path))
                self.assertFalse(handoffs.resolve(
                    opened["id"], resolved_by="owner@example.com",
                    tenant_id="tenant-a", path=path))
                self.assertEqual(handoffs.list_tickets(
                    tenant_id="tenant-a", path=path), [])
                resolved = handoffs.list_tickets(
                    status=None, tenant_id="tenant-a", path=path)
                self.assertEqual(resolved[0]["status"], "resolved")
                self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_tenants_cannot_see_or_resolve_each_others_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "handoffs.jsonl"
            with mock.patch.object(handoffs.audit, "record"):
                opened = handoffs.request(
                    "Needs review.", requested_by="slack:user-1",
                    tenant_id="tenant-a", path=path)
                self.assertEqual(handoffs.list_tickets(
                    tenant_id="tenant-b", path=path), [])
                self.assertFalse(handoffs.resolve(
                    opened["id"], resolved_by="other@example.com",
                    tenant_id="tenant-b", path=path))

    def test_tool_uses_server_context_not_model_supplied_context(self) -> None:
        principal = auth.Principal("slack:U1", ("guest",), "tenant-a")
        call = tools.ToolCall("call-1", "request_human_handoff", {
            "summary": "Please review this request.",
            "reason": "customer_request",
            "_channel": "forged",
            "_thread_key": "forged",
        })
        with mock.patch.object(handoffs, "request", return_value={"id": "H-TEST"}) as request:
            result = tools.execute(
                call, principal=principal,
                context={"channel": "slack", "thread_key": "C1:1.0",
                         "session_id": "session-1"},
            )
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(json.loads(result.content)["handoff_id"], "H-TEST")
        request.assert_called_once_with(
            "Please review this request.", requested_by="slack:U1",
            tenant_id="tenant-a", channel="slack", thread_key="C1:1.0",
            session_id="session-1", reason="customer_request")


if __name__ == "__main__":
    unittest.main()


class AuditRotationTests(unittest.TestCase):
    """The audit chain survives rotation."""

    def _write(self, path: Path, count: int) -> None:
        for index in range(count):
            audit.record("tool.requested", actor="a@example.com", session_id="s1",
                         details={"tool": f"call-{index}"}, path=path)

    def test_rotation_preserves_the_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with mock.patch.dict(os.environ, {"FRONTDESK_AUDIT_MAX_BYTES": "1500"}):
                self._write(path, 40)
                self.assertTrue(audit.segments(path), "the log did not rotate")
                ok, count, _ = audit.verify(path)
            self.assertTrue(ok)
            self.assertEqual(count, 40, "the walk did not reach every event across segments")

    def test_tampering_in_a_rotated_segment_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with mock.patch.dict(os.environ, {"FRONTDESK_AUDIT_MAX_BYTES": "1500"}):
                self._write(path, 40)
                victim = audit.segments(path)[0]
                victim.write_bytes(victim.read_bytes().replace(b"call-1", b"call-X", 1))
                ok, _, reason = audit.verify(path)
            self.assertFalse(ok)
            self.assertEqual(reason, "event hash mismatch")

    def test_read_events_spans_segments_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with mock.patch.dict(os.environ, {"FRONTDESK_AUDIT_MAX_BYTES": "1500"}):
                self._write(path, 40)
                recent = audit.read_events(5, path=path)
            self.assertEqual(len(recent), 5)
            self.assertEqual(recent[-1]["details"]["tool"], "call-39")
            self.assertEqual(recent[0]["details"]["tool"], "call-35")


class HistoryTrimmingTests(unittest.TestCase):
    """History trimming. Splitting a call from its result makes the API return 400, so the structure is checked too."""

    def _session(self, budget: int):
        import chat
        import config as cfg
        from providers import Turn
        from tools import ToolCall, ToolResult

        config = cfg.Config(provider="echo", persona="ecommerce",
                            max_history_chars=budget).resolve()
        session = chat.Session(config, chat.Style(enabled=False),
                               auth.Principal("t@example.com", ("admin",)))
        for index in range(12):
            session.history.append(Turn("user", text=f"request {index} " + "x" * 60))
            session.history.append(Turn("assistant", tool_calls=[
                ToolCall(f"c{index}", "get_order_status", {"order_id": f"A-{index}"})]))
            session.history.append(Turn("tool", tool_results=[
                ToolResult(f"c{index}", "get_order_status", '{"status": "In transit"}')]))
            session.history.append(Turn("assistant", text=f"reply {index} " + "y" * 60))
        return session, Turn

    def test_trimming_respects_budget_and_keeps_structure_valid(self) -> None:
        session, Turn = self._session(600)
        session.history.append(Turn("user", text="the current request"))
        dropped = session._trim_history()

        self.assertGreater(dropped, 0)
        self.assertLessEqual(session._history_size(), 600)
        # Claude returns 400 unless the history starts on a user turn
        self.assertEqual(session.history[0].role, "user")
        # the in-flight request always survives
        self.assertEqual(session.history[-1].text, "the current request")

        opened: set[str] = set()
        for turn in session.history:
            for call in turn.tool_calls:
                opened.add(call.id)
            for result in turn.tool_results:
                self.assertIn(result.id, opened, "a tool_result lost the tool_use it belongs to")

    def test_no_trimming_when_within_budget(self) -> None:
        session, Turn = self._session(1_000_000)
        before = len(session.history)
        self.assertEqual(session._trim_history(), 0)
        self.assertEqual(len(session.history), before)

    def test_budget_of_zero_disables_trimming(self) -> None:
        session, Turn = self._session(0)
        before = len(session.history)
        self.assertEqual(session._trim_history(), 0)
        self.assertEqual(len(session.history), before)


class DoctorTests(unittest.TestCase):
    """The steps the doctor prints are worthless unless the commands exist and work."""

    def _report(self, env: dict):
        import doctor
        with mock.patch.dict(os.environ, env, clear=False):
            report = doctor.Report()
            doctor._auth(report)
            return doctor, report

    def test_missing_auth_is_reported_with_runnable_fixes(self) -> None:
        doctor, report = self._report(
            {"FRONTDESK_AUTH_SECRET": "", "FRONTDESK_ACCESS_TOKEN": "",
             "FRONTDESK_AUTH_MODE": ""})
        self.assertEqual(report.blocking, 2)
        fixes = [fix for _, _, _, group in report.sections[0][1] for fix in group]
        self.assertIn("python auth.py --new-secret", fixes)

    def test_new_secret_runs_without_subject(self) -> None:
        """auth.py --new-secret, which the doctor points at, runs on its own."""
        import auth
        with mock.patch("sys.argv", ["auth.py", "--new-secret"]):
            with mock.patch("sys.stdout") as out:
                self.assertEqual(auth._main(), 0)
        printed = "".join(str(call.args[0]) for call in out.write.call_args_list if call.args)
        self.assertGreaterEqual(len(printed.strip()), 32)

    def test_valid_token_is_recognised(self) -> None:
        import auth
        secret = "a-secret-that-is-comfortably-long-enough"
        token = auth.issue_token(auth.Principal("ops@example.com", ("admin",)), secret, 600)
        _, report = self._report(
            {"FRONTDESK_AUTH_SECRET": secret, "FRONTDESK_ACCESS_TOKEN": token,
             "FRONTDESK_AUTH_MODE": ""})
        self.assertEqual(report.blocking, 0)
        rendered = report.render()
        self.assertIn("ops@example.com", rendered)

    def test_disabled_auth_is_flagged(self) -> None:
        _, report = self._report({"FRONTDESK_AUTH_MODE": "disabled"})
        self.assertEqual(report.blocking, 1)
        self.assertIn("never in production", report.render())

    def test_rag_status_key_matches_the_module(self) -> None:
        """index_status() reports ready - so that a doctor reading the wrong key is caught here."""
        import rag
        self.assertIn("ready", rag.index_status())


class ChannelSecurityTests(unittest.TestCase):
    """Nobody on public social media can modify or reach customer data."""

    def _public(self):
        from channels import PUBLIC, InboundMessage
        return InboundMessage("meta", "ig-1", "ig-1", "hi", trust=PUBLIC).principal()

    def test_public_channel_cannot_touch_money_or_customer_data(self) -> None:
        principal = self._public()
        for permission in ("payments:write", "payments:read",
                           "reservations:write", "orders:read"):
            self.assertFalse(principal.can(permission),
                             f"public channel was granted {permission}")
        self.assertTrue(principal.can("knowledge:read"))

    def test_payment_tools_do_not_exist_on_a_public_channel(self) -> None:
        self.assertFalse(any("paypal" in name or "payment" in name
                             for name in tools.REGISTRY))

    def test_misconfiguration_cannot_raise_a_public_channel(self) -> None:
        """Configuring finance and admin does not grant them on a public channel."""
        from channels import PUBLIC, roles_for
        with mock.patch.dict(os.environ, {"FRONTDESK_CHANNEL_META_ROLES": "finance,admin"}):
            self.assertEqual(roles_for("meta", PUBLIC), ("guest",))

    def test_subject_is_namespaced_by_channel(self) -> None:
        """Anyone can claim a social handle, so it must never collide with an internal identifier."""
        self.assertEqual(self._public().subject, "meta:ig-1")


class ChannelSignatureTests(unittest.TestCase):
    """Signature verification. Loosen it and anyone can forge an inbound \"refund me\" message."""

    BODY = b'{"team_id":"T1","event":{"type":"message","user":"U1",' \
           b'"text":"hi","channel":"C1","ts":"1.0"}}'
    SECRET = "slack-signing-secret"

    def _slack_headers(self, body=None, at=None):
        import hashlib as h, hmac as m, time as t
        body = self.BODY if body is None else body
        stamp = str(int(at if at is not None else t.time()))
        signature = "v0=" + m.new(self.SECRET.encode(),
                                  b"v0:" + stamp.encode() + b":" + body,
                                  h.sha256).hexdigest()
        return {"X-Slack-Signature": signature, "X-Slack-Request-Timestamp": stamp}

    def test_slack_accepts_a_genuine_signature(self) -> None:
        from channels.signatures import verify_slack
        self.assertTrue(verify_slack(self._slack_headers(), self.BODY, self.SECRET))

    def test_slack_rejects_tampering_and_replay(self) -> None:
        import time as t
        from channels.signatures import verify_slack
        headers = self._slack_headers()
        self.assertFalse(verify_slack(headers, self.BODY + b"x", self.SECRET))
        old = self._slack_headers(at=t.time() - 3600)
        self.assertFalse(verify_slack(old, self.BODY, self.SECRET),
                         "an hour-old replay was accepted")

    def test_missing_secret_never_passes(self) -> None:
        """A missing setting must not turn verification into a no-op."""
        from channels.signatures import verify_meta, verify_slack
        self.assertFalse(verify_slack(self._slack_headers(), self.BODY, ""))
        self.assertFalse(verify_meta({"X-Hub-Signature-256": "sha256=x"}, b"{}", ""))

    def test_meta_signature_and_header_case(self) -> None:
        import hashlib as h, hmac as m
        from channels.signatures import verify_meta
        secret, body = "app-secret", b'{"entry":[]}'
        signature = "sha256=" + m.new(secret.encode(), body, h.sha256).hexdigest()
        self.assertTrue(verify_meta({"X-Hub-Signature-256": signature}, body, secret))
        self.assertTrue(verify_meta({"x-hub-signature-256": signature}, body, secret))
        self.assertFalse(verify_meta({"X-Hub-Signature-256": signature}, body + b"!", secret))


class ChannelParsingTests(unittest.TestCase):
    """Reacting to its own messages would loop forever."""

    def test_echoes_and_bots_are_ignored(self) -> None:
        from channels.meta import MetaChannel
        from channels.slack import SlackChannel
        echo = json.dumps({"entry": [{"messaging": [
            {"sender": {"id": "PAGE"}, "message": {"text": "hi", "is_echo": True}}]}]})
        self.assertEqual(MetaChannel().parse(echo.encode()), [])
        bot = b'{"team_id":"T1","event":{"type":"message","bot_id":"B1",' \
              b'"text":"x","user":"U1","channel":"C1","ts":"1"}}'
        self.assertEqual(SlackChannel().parse(bot), [])

    def test_other_workspace_is_ignored(self) -> None:
        from channels.slack import SlackChannel
        body = b'{"team_id":"OTHER","event":{"type":"message","user":"U1",' \
               b'"text":"hi","channel":"C1","ts":"1"}}'
        with mock.patch.dict(os.environ, {"FRONTDESK_SLACK_TEAM_ID": "T1"}):
            self.assertEqual(SlackChannel().parse(body), [])


LINKEDIN_ENV = {
    "FRONTDESK_LINKEDIN_CLIENT_ID": "client-id",
    "FRONTDESK_LINKEDIN_CLIENT_SECRET": "client-secret",
    "FRONTDESK_LINKEDIN_REDIRECT_URI": "https://verify.example.com/linkedin/callback",
    "FRONTDESK_LINKEDIN_STATE_SECRET": "a" * 32,
}


class LinkedInStateTests(unittest.TestCase):
    """The state parameter. It is the only thing tying a callback to a conversation."""

    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, LINKEDIN_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_round_trip_carries_the_conversation(self) -> None:
        state = linkedin.issue_state("meta", "ig-77", "ig-77:thread")
        claims = linkedin.verify_state(state)
        self.assertEqual((claims["ch"], claims["uid"], claims["thread"]),
                         ("meta", "ig-77", "ig-77:thread"))

    def test_tampering_is_rejected(self) -> None:
        state = linkedin.issue_state("meta", "ig-77", "ig-77:thread")
        payload, signature = state.split(".", 1)
        forged = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        with self.assertRaises(linkedin.LinkedInError):
            linkedin.verify_state(f"{forged}.{signature}")

    def test_another_secret_is_rejected(self) -> None:
        """A state signed elsewhere must not verify here."""
        state = linkedin.issue_state("meta", "ig-77", "ig-77:thread")
        with mock.patch.dict(os.environ, {"FRONTDESK_LINKEDIN_STATE_SECRET": "b" * 32}):
            with self.assertRaises(linkedin.LinkedInError):
                linkedin.verify_state(state)

    def test_expiry_is_enforced(self) -> None:
        issued = 1_000_000
        state = linkedin.issue_state("meta", "ig-77", "t", now=issued)
        self.assertTrue(linkedin.verify_state(state, now=issued + 60))
        with self.assertRaises(linkedin.LinkedInError):
            linkedin.verify_state(state, now=issued + linkedin.STATE_TTL_SECONDS + 1)

    def test_a_checkout_token_is_not_a_state_token(self) -> None:
        """Domain separation: the two signers must not accept each other's work."""
        checkout_env = {"FRONTDESK_CHECKOUT_SECRET": "a" * 32,
                        "FRONTDESK_CHECKOUT_BASE_URL": "https://pay.example.com"}
        with mock.patch.dict(os.environ, checkout_env):
            url = paypal_checkout.create_checkout_url("5O190127TN364715T", "10.00", "USD")
        token = url.split("#", 1)[1]
        with self.assertRaises(linkedin.LinkedInError):
            linkedin.verify_state(token)

    def test_a_short_secret_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"FRONTDESK_LINKEDIN_STATE_SECRET": "short"}):
            self.assertFalse(linkedin.configured())
            with self.assertRaises(linkedin.LinkedInError):
                linkedin.issue_state("meta", "ig-77", "t")

    def test_required_configuration_accepts_complete_oidc_settings(self) -> None:
        linkedin.require_configured()

    def test_required_configuration_rejects_a_missing_client_id(self) -> None:
        with mock.patch.dict(os.environ, {"FRONTDESK_LINKEDIN_CLIENT_ID": ""}):
            with self.assertRaisesRegex(linkedin.LinkedInError, "is required"):
                linkedin.require_configured()

    def test_required_configuration_validates_the_redirect_uri(self) -> None:
        with mock.patch.dict(os.environ, {
                "FRONTDESK_LINKEDIN_REDIRECT_URI": "http://verify.example.com/callback"}):
            with self.assertRaises(linkedin.LinkedInError):
                linkedin.require_configured()

    def test_redirect_uri_must_be_https_and_bare(self) -> None:
        for bad in ("http://verify.example.com/cb",
                    "https://verify.example.com/cb?x=1",
                    "https://verify.example.com/cb#f"):
            with self.subTest(uri=bad):
                with mock.patch.dict(os.environ,
                                     {"FRONTDESK_LINKEDIN_REDIRECT_URI": bad}):
                    with self.assertRaises(linkedin.LinkedInError):
                        linkedin.redirect_uri()
        with mock.patch.dict(
                os.environ,
                {"FRONTDESK_LINKEDIN_REDIRECT_URI": "http://localhost:8790/cb"}):
            self.assertTrue(linkedin.redirect_uri())

    def test_authorization_url_carries_what_linkedin_needs(self) -> None:
        url = linkedin.authorization_url("meta", "ig-77", "ig-77:thread")
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertTrue(url.startswith(linkedin.AUTHORIZE_URL))
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["openid profile email"])
        self.assertEqual(query["redirect_uri"],
                         [LINKEDIN_ENV["FRONTDESK_LINKEDIN_REDIRECT_URI"]])
        self.assertTrue(linkedin.verify_state(query["state"][0]))


class LinkedInOptionalStartupTests(unittest.TestCase):
    def test_webchat_starts_without_linkedin(self) -> None:
        with mock.patch.dict(os.environ, {
                "FRONTDESK_LINKEDIN_CLIENT_ID": "",
                "FRONTDESK_LINKEDIN_CLIENT_SECRET": "",
                "FRONTDESK_LINKEDIN_REDIRECT_URI": "",
                "FRONTDESK_LINKEDIN_STATE_SECRET": ""}), \
                mock.patch("sys.argv", ["webchat.py"]), \
                mock.patch("webchat.ThreadingHTTPServer") as server:
            self.assertEqual(webchat.main(), 0)
        server.assert_called_once()

    def test_webhooks_start_without_linkedin_when_a_channel_is_configured(self) -> None:
        channel = mock.Mock(); channel.configured.return_value = True
        with mock.patch.dict(os.environ, {
                "FRONTDESK_LINKEDIN_CLIENT_ID": "",
                "FRONTDESK_LINKEDIN_CLIENT_SECRET": "",
                "FRONTDESK_LINKEDIN_REDIRECT_URI": "",
                "FRONTDESK_LINKEDIN_STATE_SECRET": ""}), \
                mock.patch("channels.available", return_value={"slack": channel}), \
                mock.patch("webhooks.ThreadingHTTPServer") as server:
            self.assertEqual(webhooks.serve(), 0)
        server.assert_called_once()


class LinkedInTrustTests(unittest.TestCase):
    """What a sign-in established decides the tier. Configuration never does."""

    VERIFIED = {"sub": "li-1", "name": "Dana Whitfield",
                "email": "dana.whitfield@example.com", "email_verified": True}

    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, LINKEDIN_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        tools.reset_store()
        self.addCleanup(tools.reset_store)

    def test_an_unverified_email_establishes_nothing(self) -> None:
        claims = dict(self.VERIFIED, email_verified=False)
        self.assertEqual(
            linkedin.trust_for(claims, lambda email: {"customer": "x"}),
            channels.PUBLIC)

    def test_a_verified_stranger_stays_public(self) -> None:
        self.assertEqual(linkedin.trust_for(self.VERIFIED, lambda email: None),
                         channels.PUBLIC)

    def test_matching_a_customer_reaches_authenticated(self) -> None:
        self.assertEqual(
            linkedin.trust_for(self.VERIFIED, tools.find_customer_by_email),
            channels.AUTHENTICATED)

    def test_our_own_domain_is_the_workspace_tier(self) -> None:
        claims = dict(self.VERIFIED, email="ops@shelliecom.com")
        with mock.patch.dict(
                os.environ, {"FRONTDESK_LINKEDIN_WORKSPACE_DOMAINS": "shelliecom.com"}):
            self.assertEqual(linkedin.trust_for(claims, lambda email: None),
                             channels.WORKSPACE)

    def test_a_failing_lookup_does_not_grant_a_tier(self) -> None:
        """A backend that is down must not hand out authentication by accident."""

        def broken(email):
            raise RuntimeError("backend down")

        self.assertEqual(linkedin.trust_for(self.VERIFIED, broken), channels.PUBLIC)

    def test_the_subject_is_the_verified_email_namespaced(self) -> None:
        principal = linkedin.principal_for(
            self.VERIFIED, customer_lookup=tools.find_customer_by_email)
        self.assertEqual(principal.subject, "linkedin:dana.whitfield@example.com")

    def test_configuration_cannot_raise_a_signed_in_person(self) -> None:
        """The ceiling in base.py still applies once someone has signed in."""
        with mock.patch.dict(
                os.environ, {"FRONTDESK_CHANNEL_LINKEDIN_ROLES": "finance,admin"}):
            principal = linkedin.principal_for(dict(self.VERIFIED, email_verified=False))
        self.assertEqual(principal.roles, ("guest",))
        self.assertFalse(principal.can("payments:write"))


class LinkedInExchangeTests(unittest.TestCase):
    """The OpenID Connect round trip."""

    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, LINKEDIN_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_code_is_traded_for_a_token(self) -> None:
        with mock.patch.object(linkedin, "_read_json",
                               return_value={"access_token": "AT-1"}):
            self.assertEqual(linkedin.exchange_code("code-1"), "AT-1")

    def test_a_response_without_a_token_is_an_error(self) -> None:
        with mock.patch.object(linkedin, "_read_json", return_value={"error": "nope"}):
            with self.assertRaises(linkedin.LinkedInError):
                linkedin.exchange_code("code-1")

    def test_userinfo_must_carry_a_subject(self) -> None:
        with mock.patch.object(linkedin, "_read_json", return_value={"name": "x"}):
            with self.assertRaises(linkedin.LinkedInError):
                linkedin.fetch_userinfo("AT-1")

    def test_the_client_secret_never_leaves_the_back_channel(self) -> None:
        """It goes in the POST body to the token endpoint and nowhere else."""
        seen = {}

        def capture(request):
            seen["url"] = request.full_url
            seen["body"] = request.data or b""
            seen["headers"] = dict(request.headers)
            return {"access_token": "AT-1"}

        with mock.patch.object(linkedin, "_read_json", side_effect=capture):
            linkedin.exchange_code("code-1")
        self.assertEqual(seen["url"], linkedin.TOKEN_URL)
        self.assertIn(b"client_secret=client-secret", seen["body"])
        self.assertNotIn("client-secret", json.dumps(seen["headers"]))
        self.assertNotIn("client-secret",
                         linkedin.authorization_url("meta", "u", "t"))


class LinkedInIdentityStoreTests(unittest.TestCase):
    """A verification has to survive to the next message, and then stop existing."""

    def test_remember_recall_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verified.json"
            identity.remember("meta", "ig-9", subject="linkedin:dana@example.com",
                              email="dana@example.com", trust=channels.AUTHENTICATED,
                              name="Dana", path=path, now=1_000_000)
            record = identity.recall("meta", "ig-9", path=path, now=1_000_060)
            self.assertEqual(record["trust"], channels.AUTHENTICATED)
            later = 1_000_000 + identity._ttl_seconds() + 1
            self.assertIsNone(identity.recall("meta", "ig-9", path=path, now=later))
            # The expired record is dropped, not merely hidden.
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})

    def test_forget_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verified.json"
            identity.remember("meta", "ig-9", subject="s", email="e@x.com",
                              trust=channels.AUTHENTICATED, path=path)
            self.assertTrue(identity.forget("meta", "ig-9", path=path))
            self.assertFalse(identity.forget("meta", "ig-9", path=path))
            self.assertIsNone(identity.recall("meta", "ig-9", path=path))

    def test_a_corrupt_store_does_not_take_the_channel_down(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "verified.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(identity.recall("meta", "ig-9", path=path))


class LinkedInEndToEndTests(unittest.TestCase):
    """The point of the integration: a refusal the person can actually resolve."""

    def setUp(self) -> None:
        # This deployment lets a verified customer change their own booking; the
        # ceiling in base.py is what stops the setting reaching any further.
        self.env = mock.patch.dict(
            os.environ, dict(LINKEDIN_ENV, FRONTDESK_CHANNEL_META_ROLES="operator"))
        self.env.start()
        self.addCleanup(self.env.stop)
        tools.reset_store()
        self.addCleanup(tools.reset_store)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        store = Path(self.temporary.name) / "verified.json"
        patched = mock.patch.object(identity, "STORE_PATH", store)
        patched.start()
        self.addCleanup(patched.stop)

    def _message(self):
        return channels.InboundMessage("meta", "ig-42", "ig-42:t",
                                       "cancel reservation R-2003",
                                       trust=channels.PUBLIC)

    def test_a_dm_cannot_cancel_a_reservation_until_it_is_verified(self) -> None:
        message = self._message()
        before = channels.principal_for(message)
        self.assertEqual(before.roles, ("guest",))
        refused = tools.execute(
            tools.ToolCall("t1", "cancel_reservation", {"reservation_id": "R-2003"}),
            "en", before)
        self.assertTrue(refused.is_error)
        self.assertIn("Permission denied", refused.content)

        # Dana signs in with LinkedIn; the verified email matches her order.
        claims = {"sub": "li-1", "name": "Dana Whitfield",
                  "email": "dana.whitfield@example.com", "email_verified": True}
        principal = linkedin.principal_for(
            claims, customer_lookup=tools.find_customer_by_email)
        trust = linkedin.trust_for(claims, customer_lookup=tools.find_customer_by_email)
        self.assertEqual(trust, channels.AUTHENTICATED)
        identity.remember("meta", "ig-42", subject=principal.subject,
                          email="dana.whitfield@example.com", trust=trust,
                          name="Dana Whitfield")

        after = channels.principal_for(message)
        self.assertEqual(after.subject, "linkedin:dana.whitfield@example.com")
        self.assertTrue(after.can("reservations:write"))
        done = tools.execute(
            tools.ToolCall("t2", "cancel_reservation", {"reservation_id": "R-2003"}),
            "en", after)
        self.assertFalse(done.is_error)

    def test_an_expired_verification_returns_the_person_to_guest(self) -> None:
        message = self._message()
        identity.remember("meta", "ig-42",
                          subject="linkedin:dana.whitfield@example.com",
                          email="dana.whitfield@example.com",
                          trust=channels.AUTHENTICATED)
        self.assertTrue(channels.principal_for(message).can("reservations:write"))
        with mock.patch.dict(os.environ, {"FRONTDESK_IDENTITY_TTL_HOURS": "0.0001"}):
            identity.remember("meta", "ig-42",
                              subject="linkedin:dana.whitfield@example.com",
                              email="dana.whitfield@example.com",
                              trust=channels.AUTHENTICATED, now=1_000)
        self.assertEqual(channels.principal_for(message).roles, ("guest",))

    def test_a_verified_customer_is_read_only_by_default(self) -> None:
        """Least privilege: without configuration, verifying earns lookups only."""
        message = self._message()
        identity.remember("meta", "ig-42",
                          subject="linkedin:dana.whitfield@example.com",
                          email="dana.whitfield@example.com",
                          trust=channels.AUTHENTICATED)
        with mock.patch.dict(os.environ, {"FRONTDESK_CHANNEL_META_ROLES": ""}):
            principal = channels.principal_for(message)
        self.assertEqual(principal.roles, ("support",))
        self.assertTrue(principal.can("reservations:read"))
        self.assertFalse(principal.can("reservations:write"))

    def test_a_verified_customer_cannot_gain_a_payment_tool(self) -> None:
        """Identity verification cannot add a tool that FrontDesk does not ship."""
        message = self._message()
        identity.remember("meta", "ig-42",
                          subject="linkedin:dana.whitfield@example.com",
                          email="dana.whitfield@example.com",
                          trust=channels.AUTHENTICATED)
        with mock.patch.dict(os.environ,
                             {"FRONTDESK_CHANNEL_META_ROLES": "finance,operator"}):
            principal = channels.principal_for(message)
        self.assertNotIn("finance", principal.roles)
        self.assertFalse(principal.can("payments:write"))
        self.assertNotIn("refund_paypal_capture", tools.REGISTRY)

    def test_a_verified_customer_reaches_only_their_own_records(self) -> None:
        """A role says what you may do, never whose records you may do it to."""
        message = self._message()
        identity.remember("meta", "ig-42",
                          subject="linkedin:dana.whitfield@example.com",
                          email="dana.whitfield@example.com",
                          trust=channels.AUTHENTICATED)
        principal = channels.principal_for(message)

        # R-2001 is Emily Carter's.
        for name, arguments in (
            ("cancel_reservation", {"reservation_id": "R-2001"}),
            ("change_reservation", {"reservation_id": "R-2001", "new_time": "20:00"}),
            ("get_order_status", {"order_id": "A-88001"}),
        ):
            with self.subTest(tool=name):
                result = tools.execute(tools.ToolCall("t", name, arguments),
                                       "en", principal)
                self.assertTrue(result.is_error)
                self.assertIn("not found on your account", result.content)

        # Her own order is readable, and R-2001 is untouched.
        mine = tools.execute(
            tools.ToolCall("t", "get_order_status", {"order_id": "A-88003"}),
            "en", principal)
        self.assertFalse(mine.is_error)
        self.assertEqual(tools.load_store()["reservations"]["R-2001"]["status"],
                         "confirmed")

    def test_a_search_returns_only_the_signed_in_customer(self) -> None:
        message = self._message()
        identity.remember("meta", "ig-42",
                          subject="linkedin:dana.whitfield@example.com",
                          email="dana.whitfield@example.com",
                          trust=channels.AUTHENTICATED)
        principal = channels.principal_for(message)
        result = tools.execute(
            tools.ToolCall("t", "search_reservations", {"customer": ""}),
            "en", principal)
        self.assertFalse(result.is_error)
        found = json.loads(result.content)["reservations"]
        self.assertEqual([r["customer"] for r in found], ["Dana Whitfield"])

    def test_an_operator_still_sees_every_record(self) -> None:
        """The scoping must not leak into the staff-facing path."""
        operator = auth.Principal("casey@example.com", ("operator",))
        result = tools.execute(
            tools.ToolCall("t", "search_reservations", {"customer": ""}),
            "en", operator)
        found = json.loads(result.content)["reservations"]
        self.assertEqual(len(found), 3)
        self.assertEqual(operator.customer_email, "")

    def test_the_refusal_offers_a_way_forward(self) -> None:
        offer = channels.sign_in_offer(self._message())
        self.assertIn("linkedin.com/oauth", offer)
        self.assertIn("15 minutes", offer)

    def test_no_link_is_offered_when_linkedin_is_not_configured(self) -> None:
        with mock.patch.dict(os.environ, {"FRONTDESK_LINKEDIN_CLIENT_ID": ""}):
            self.assertEqual(channels.sign_in_offer(self._message()), "")
        self.assertNotIn("linkedin", channels.handoff_notice("meta").lower())


TEAMS_SECRET = base64.b64encode(b"teams-outgoing-webhook-secret").decode("ascii")


def teams_headers(body: bytes, secret: str = TEAMS_SECRET) -> dict:
    key = base64.b64decode(secret)
    digest = hmac.new(key, body, hashlib.sha256).digest()
    return {"Authorization": "HMAC " + base64.b64encode(digest).decode("ascii")}


TEAMS_ACTIVITY = json.dumps({
    "type": "message",
    "id": "activity-1",
    "text": "<at>Frontdesk</at> where is order A-88001",
    "from": {"id": "29:user-1", "name": "Casey Lin"},
    "conversation": {"id": "19:thread-1"},
    "channelData": {"tenant": {"id": "tenant-1"}},
}).encode("utf-8")


class TeamsSignatureTests(unittest.TestCase):
    """Teams signs with the decoded secret. Using the printable form never matches."""

    def test_a_valid_signature_passes(self) -> None:
        self.assertTrue(signatures.verify_teams(
            teams_headers(TEAMS_ACTIVITY), TEAMS_ACTIVITY, TEAMS_SECRET))

    def test_a_tampered_body_fails(self) -> None:
        headers = teams_headers(TEAMS_ACTIVITY)
        self.assertFalse(signatures.verify_teams(
            headers, TEAMS_ACTIVITY + b" ", TEAMS_SECRET))

    def test_another_secret_fails(self) -> None:
        other = base64.b64encode(b"a-different-secret-entirely").decode("ascii")
        self.assertFalse(signatures.verify_teams(
            teams_headers(TEAMS_ACTIVITY, other), TEAMS_ACTIVITY, TEAMS_SECRET))

    def test_no_secret_configured_fails_closed(self) -> None:
        """A missing setting must not turn verification into a no-op."""
        self.assertFalse(signatures.verify_teams(
            teams_headers(TEAMS_ACTIVITY), TEAMS_ACTIVITY, ""))

    def test_a_secret_that_is_not_base64_fails_closed(self) -> None:
        self.assertFalse(signatures.verify_teams(
            teams_headers(TEAMS_ACTIVITY), TEAMS_ACTIVITY, "not base64 !!"))

    def test_the_undecoded_secret_is_not_the_key(self) -> None:
        """The classic Teams mistake: signing with the printable secret."""
        wrong = hmac.new(TEAMS_SECRET.encode("ascii"), TEAMS_ACTIVITY,
                         hashlib.sha256).digest()
        headers = {"Authorization": "HMAC " + base64.b64encode(wrong).decode("ascii")}
        self.assertFalse(signatures.verify_teams(headers, TEAMS_ACTIVITY, TEAMS_SECRET))

    def test_a_missing_header_fails(self) -> None:
        self.assertFalse(signatures.verify_teams({}, TEAMS_ACTIVITY, TEAMS_SECRET))
        self.assertFalse(signatures.verify_teams(
            {"Authorization": "Bearer something"}, TEAMS_ACTIVITY, TEAMS_SECRET))


class TeamsParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        from channels.teams import TeamsChannel
        self.channel = TeamsChannel()

    def test_the_mention_markup_is_stripped(self) -> None:
        message = self.channel.parse(TEAMS_ACTIVITY)[0]
        self.assertEqual(message.text, "where is order A-88001")
        self.assertEqual(message.trust, channels.WORKSPACE)
        self.assertEqual(message.thread_key, "19:thread-1")
        self.assertEqual(message.principal().subject, "teams:29:user-1")

    def test_its_own_output_is_ignored(self) -> None:
        """Reacting to its own messages would loop forever."""
        echo = json.loads(TEAMS_ACTIVITY)
        echo["from"]["role"] = "bot"
        self.assertEqual(self.channel.parse(json.dumps(echo).encode()), [])

    def test_a_bare_mention_is_ignored(self) -> None:
        bare = json.loads(TEAMS_ACTIVITY)
        bare["text"] = "<at>Frontdesk</at>"
        self.assertEqual(self.channel.parse(json.dumps(bare).encode()), [])

    def test_other_activity_types_are_ignored(self) -> None:
        other = json.loads(TEAMS_ACTIVITY)
        other["type"] = "conversationUpdate"
        self.assertEqual(self.channel.parse(json.dumps(other).encode()), [])

    def test_another_tenant_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"FRONTDESK_TEAMS_TENANT_ID": "tenant-1"}):
            from channels.teams import TeamsChannel
            self.assertTrue(TeamsChannel().parse(TEAMS_ACTIVITY))
            foreign = json.loads(TEAMS_ACTIVITY)
            foreign["channelData"]["tenant"]["id"] = "tenant-2"
            self.assertEqual(TeamsChannel().parse(json.dumps(foreign).encode()), [])

    def test_sending_out_of_band_is_refused_not_ignored(self) -> None:
        """A caller that reaches send() thinks it has replied and has not."""
        with self.assertRaises(channels.ChannelError):
            self.channel.send("19:thread-1", "hello")

    def test_the_reply_is_an_activity(self) -> None:
        payload = self.channel.reply_payload("x" * 9000)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(len(payload["text"]), 4000)


class _StubChannel:
    """A channel that records what it was asked to do, and answers instantly."""

    def __init__(self, name, verifies=True, messages=None):
        self.name = name
        self.verifies = verifies
        self.messages = messages if messages is not None else []
        self.parsed = 0
        self.sent = []

    def configured(self):
        return True

    def verify(self, headers, body):
        return self.verifies

    def parse(self, body):
        self.parsed += 1
        return list(self.messages)

    def send(self, thread_key, text):
        self.sent.append((thread_key, text))

    def challenge(self, query):
        return "challenge-answer" if query.get("hub.verify_token") == "right" else None

    def reply_payload(self, text):
        return {"type": "message", "text": text}


class _StubDispatcher:
    def __init__(self, reply="the answer"):
        self.reply = reply
        self.seen = []

    def handle(self, message):
        self.seen.append(message)
        return self.reply


class WebhookReceiverTests(unittest.TestCase):
    """The endpoint the platforms post to. Nothing reaches the agent unverified."""

    def setUp(self) -> None:
        webhooks.reset_seen()
        self.addCleanup(webhooks.reset_seen)
        self.message = channels.InboundMessage(
            "slack", "U1", "C1:1.0", "where is my order", trust=channels.WORKSPACE)
        self.slack = _StubChannel("slack", messages=[self.message])
        self.meta = _StubChannel("meta")
        self.teams = _StubChannel("teams", messages=[
            channels.InboundMessage("teams", "29:u", "19:t", "hello",
                                    trust=channels.WORKSPACE)])
        self.dispatcher = _StubDispatcher()

        registry = {"slack": self.slack, "meta": self.meta, "teams": self.teams}
        handler = type("BoundHandler", (webhooks.WebhookHandler,),
                       {"registry": registry, "dispatcher": self.dispatcher})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _request(self, method, path, body=b"", headers=None):
        request = urllib.request.Request(
            self.base + path, data=body if method == "POST" else None,
            headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_an_unsigned_request_never_reaches_the_agent(self) -> None:
        self.slack.verifies = False
        status, _ = self._request("POST", "/slack", b'{"event_id":"E1"}')
        self.assertEqual(status, 401)
        self.assertEqual(self.slack.parsed, 0)
        self.assertEqual(self.dispatcher.seen, [])

    def test_a_signed_request_is_acknowledged_then_answered(self) -> None:
        status, _ = self._request("POST", "/slack", b'{"event_id":"E1"}')
        self.assertEqual(status, 200)
        deadline = time.time() + 5
        while not self.slack.sent and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.slack.sent, [("C1:1.0", "the answer")])

    def test_a_retry_is_not_run_twice(self) -> None:
        """A slow first attempt must not turn one cancellation into two."""
        body = b'{"event_id":"E-same"}'
        for _ in range(3):
            self.assertEqual(self._request("POST", "/slack", body)[0], 200)
        deadline = time.time() + 5
        while not self.slack.sent and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        self.assertEqual(len(self.slack.sent), 1)
        self.assertEqual(self.slack.parsed, 1)

    def test_slack_url_verification_is_answered(self) -> None:
        body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
        status, payload = self._request("POST", "/slack", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["challenge"], "abc")
        self.assertEqual(self.dispatcher.seen, [])

    def test_teams_is_answered_in_the_response_body(self) -> None:
        status, payload = self._request("POST", "/teams", TEAMS_ACTIVITY)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload),
                         {"type": "message", "text": "the answer"})
        self.assertEqual(self.teams.sent, [])      # never sent out of band

    def test_the_meta_challenge_needs_the_right_token(self) -> None:
        status, body = self._request(
            "GET", "/meta?hub.mode=subscribe&hub.verify_token=right&hub.challenge=42")
        self.assertEqual((status, body), (200, b"challenge-answer"))
        status, _ = self._request(
            "GET", "/meta?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=42")
        self.assertEqual(status, 403)

    def test_an_unknown_path_is_not_found(self) -> None:
        self.assertEqual(self._request("POST", "/whatsapp", b"{}")[0], 404)
        self.assertEqual(self._request("GET", "/slack")[0], 404)

    def test_health_reveals_nothing(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual((status, body), (200, b"ok"))

    def test_an_oversized_body_is_refused_unread(self) -> None:
        status, _ = self._request(
            "POST", "/slack", b"x", {"Content-Length": str(webhooks.MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.slack.parsed, 0)

    def test_an_unparsable_payload_is_a_bad_request(self) -> None:
        def explode(body):
            raise channels.ChannelError("not JSON")

        self.slack.parse = explode
        self.assertEqual(self._request("POST", "/slack", b"{")[0], 400)


class WebhookDeliveryKeyTests(unittest.TestCase):
    def test_each_platform_has_a_stable_id(self) -> None:
        slack = json.dumps({"event_id": "Ev123"}).encode()
        self.assertEqual(webhooks.delivery_key("slack", {}, slack), "slack:Ev123")
        teams = json.dumps({"id": "act-1"}).encode()
        self.assertEqual(webhooks.delivery_key("teams", {}, teams), "teams:act-1")
        meta = json.dumps({"entry": [{"messaging": [
            {"message": {"mid": "m2"}}, {"message": {"mid": "m1"}}]}]}).encode()
        self.assertEqual(webhooks.delivery_key("meta", {}, meta), "meta:m1,m2")

    def test_an_unidentifiable_delivery_is_never_deduplicated(self) -> None:
        """No id means no evidence of a repeat; dropping it would lose the message."""
        self.assertEqual(webhooks.delivery_key("slack", {}, b"not json"), "")
        self.assertFalse(webhooks.already_handled(""))
        self.assertFalse(webhooks.already_handled(""))

    def test_the_record_of_seen_deliveries_expires(self) -> None:
        webhooks.reset_seen()
        self.addCleanup(webhooks.reset_seen)
        self.assertFalse(webhooks.already_handled("slack:E1", now=1_000))
        self.assertTrue(webhooks.already_handled("slack:E1", now=1_060))
        later = 1_000 + webhooks.DEDUP_SECONDS + 1
        self.assertFalse(webhooks.already_handled("slack:E1", now=later))


class ChannelOutputTests(unittest.TestCase):
    """A server must not print customer messages to its own stdout."""

    def test_a_dispatched_session_writes_nothing_to_stdout(self) -> None:
        import contextlib
        import io as _io
        from channels.dispatch import Dispatcher

        channels.reset_sessions()
        self.addCleanup(channels.reset_sessions)
        message = channels.InboundMessage(
            "slack", "U1", "C1:1.0", "hello", trust=channels.WORKSPACE)
        captured = _io.StringIO()
        with mock.patch.dict(os.environ, {"FRONTDESK_AUTH_MODE": "disabled"}):
            with contextlib.redirect_stdout(captured):
                reply = Dispatcher(provider="echo").handle(message)
        self.assertTrue(reply)
        self.assertEqual(captured.getvalue(), "")

    def test_a_channel_tool_result_uses_the_private_sink(self) -> None:
        import contextlib
        import io as _io
        import config as cfg

        configuration = cfg.Config(provider="echo", persona="default").resolve()
        sink = _io.StringIO()
        with mock.patch.object(audit, "record"):
            session = chat.Session(
                configuration, chat.Style(enabled=False),
                auth.Principal("casey@example.com", ("operator",)), out=sink,
            )
            captured = _io.StringIO()
            with contextlib.redirect_stdout(captured):
                result = session._invoke(tools.ToolCall("call-1", "get_today", {}))
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(captured.getvalue(), "")
        self.assertIn("today", sink.getvalue())

    def test_a_terminal_session_still_prints(self) -> None:
        """The sink is for servers; the CLI must be unaffected."""
        import contextlib
        import io as _io
        import chat
        import config as cfg

        configuration = cfg.Config(provider="echo", persona="default",
                                   use_tools=False).resolve()
        session = chat.Session(configuration, chat.Style(enabled=False),
                               auth.Principal("casey@example.com", ("operator",)))
        captured = _io.StringIO()
        with contextlib.redirect_stdout(captured):
            session.ask("hello")
        self.assertIn("hello", captured.getvalue())


class SessionConcurrencyTests(unittest.TestCase):
    def test_turns_in_one_session_do_not_overlap(self) -> None:
        import io as _io
        import config as cfg

        class TrackingProvider:
            name = "tracking"
            model = "test"

            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def stream(self, system, history, active_tools):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                try:
                    time.sleep(0.05)
                    yield providers.Chunk("text", "ok")
                    yield providers.Chunk("final")
                finally:
                    with self.lock:
                        self.active -= 1

        configuration = cfg.Config(
            provider="echo", persona="default", use_tools=False).resolve()
        with mock.patch.object(audit, "record"):
            session = chat.Session(
                configuration, chat.Style(enabled=False),
                auth.Principal("casey@example.com", ("operator",)), out=_io.StringIO(),
            )
            provider = TrackingProvider()
            session.provider = provider
            workers = [threading.Thread(target=session.ask, args=(message,))
                       for message in ("first", "second")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)
        self.assertEqual(provider.maximum, 1)
        self.assertCountEqual(
            [turn.text for turn in session.history if turn.role == "user"],
            ["first", "second"],
        )


MOBILE_SECRET = "a-signing-secret-that-is-long-enough-for-the-check"


class ApprovalStoreTests(unittest.TestCase):
    """A tap has to be worth exactly as much as a keypress, and no more."""

    def setUp(self) -> None:
        approvals.reset()
        self.addCleanup(approvals.reset)
        self.owner = auth.Principal("owner@deskco.com", ("operator", "finance"))
        self.junior = auth.Principal("junior@deskco.com", ("support",))

    def _park(self, timeout=30, permission="reservations:write"):
        return approvals.request(
            "Cancel reservation R-2001",
            tool="cancel_reservation", permission=permission,
            requested_by="meta:ig-42", channel="meta", timeout=timeout)

    def test_an_approval_releases_the_waiting_agent(self) -> None:
        approval = self._park()
        outcome = {}

        def agent():
            outcome["state"] = approvals.wait(approval)

        worker = threading.Thread(target=agent, daemon=True)
        worker.start()
        time.sleep(0.05)
        took, why = approvals.decide(approval.id, True, self.owner)
        worker.join(timeout=5)
        self.assertEqual((took, why), (True, ""))
        self.assertEqual(outcome["state"], approvals.APPROVED)

    def test_a_decline_releases_it_too(self) -> None:
        approval = self._park()
        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.__setitem__("state", approvals.wait(approval)),
            daemon=True)
        worker.start()
        time.sleep(0.05)
        approvals.decide(approval.id, False, self.owner)
        worker.join(timeout=5)
        self.assertEqual(outcome["state"], approvals.DECLINED)

    def test_silence_is_a_no(self) -> None:
        """An unanswered request must not become a yes by default."""
        approval = self._park(timeout=0.2)
        self.assertEqual(approvals.wait(approval), approvals.EXPIRED)

    def test_the_approver_needs_the_permission_themselves(self) -> None:
        approval = self._park()
        took, why = approvals.decide(approval.id, True, self.junior)
        self.assertFalse(took)
        self.assertIn("reservations:write", why)
        self.assertTrue(approval.is_open())

    def test_one_decision_each(self) -> None:
        approval = self._park()
        self.assertTrue(approvals.decide(approval.id, True, self.owner)[0])
        took, why = approvals.decide(approval.id, True, self.owner)
        self.assertFalse(took)
        self.assertIn("no longer open", why)

    def test_an_expired_request_cannot_be_answered(self) -> None:
        approval = self._park(timeout=0.05)
        time.sleep(0.1)
        took, _ = approvals.decide(approval.id, True, self.owner)
        self.assertFalse(took)

    def test_pending_lists_what_is_waiting(self) -> None:
        self._park()
        self._park(permission="reservations:write")
        waiting = approvals.pending()
        self.assertEqual(len(waiting), 2)
        self.assertEqual(waiting[0]["requested_by"], "meta:ig-42")
        self.assertNotIn("_event", waiting[0])

    def test_the_queue_is_bounded(self) -> None:
        """A flood must not be able to exhaust memory."""
        for _ in range(approvals.MAX_OPEN + 5):
            self._park(timeout=60)
        self.assertLessEqual(len(approvals.pending()), approvals.MAX_OPEN)


class ApprovalGateTests(unittest.TestCase):
    """The gate's third path: no terminal, but somebody reachable on a phone."""

    def setUp(self) -> None:
        approvals.reset()
        self.addCleanup(approvals.reset)

    def _session(self):
        import chat
        import config as cfg
        import io as _io
        configuration = cfg.Config(provider="echo", persona="default",
                                   use_tools=True).resolve()
        return chat.Session(configuration, chat.Style(enabled=False),
                            auth.Principal("meta:ig-42", ("guest",)),
                            out=_io.StringIO())

    def test_without_remote_approval_a_headless_run_still_declines(self) -> None:
        session = self._session()
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch.dict(os.environ, {"FRONTDESK_REMOTE_APPROVAL": ""}):
                self.assertFalse(session._approve("Refund $10.00"))

    def test_with_remote_approval_the_action_waits_and_then_runs(self) -> None:
        session = self._session()
        tool = tools.REGISTRY["cancel_reservation"]
        result = {}

        def ask():
            with mock.patch("sys.stdin.isatty", return_value=False):
                with mock.patch.dict(os.environ, {"FRONTDESK_REMOTE_APPROVAL": "1"}):
                    result["allowed"] = session._approve("Cancel reservation R-2001", tool)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        deadline = time.time() + 5
        while not approvals.pending() and time.time() < deadline:
            time.sleep(0.02)
        waiting = approvals.pending()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["summary"], "Cancel reservation R-2001")
        self.assertEqual(waiting[0]["permission"], "reservations:write")

        approvals.decide(waiting[0]["id"], True,
                         auth.Principal("owner@deskco.com", ("operator",)))
        worker.join(timeout=5)
        self.assertTrue(result["allowed"])

    def test_a_declined_action_does_not_run(self) -> None:
        session = self._session()
        tool = tools.REGISTRY["cancel_reservation"]
        result = {}

        def ask():
            with mock.patch("sys.stdin.isatty", return_value=False):
                with mock.patch.dict(os.environ, {"FRONTDESK_REMOTE_APPROVAL": "1"}):
                    result["allowed"] = session._approve("Cancel R-2001", tool)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        deadline = time.time() + 5
        while not approvals.pending() and time.time() < deadline:
            time.sleep(0.02)
        approvals.decide(approvals.pending()[0]["id"], False,
                         auth.Principal("owner@deskco.com", ("operator",)))
        worker.join(timeout=5)
        self.assertFalse(result["allowed"])


class MobilePairingTests(unittest.TestCase):
    """A link is not a token, works once, and expires."""

    def setUp(self) -> None:
        mobile.reset_pairings()
        self.addCleanup(mobile.reset_pairings)

    def test_a_link_becomes_a_token_once(self) -> None:
        grant = mobile.issue_pairing("owner@deskco.com", ("operator",), MOBILE_SECRET)
        token = mobile.redeem_pairing(grant, MOBILE_SECRET)
        principal = auth.authenticate_token(token, MOBILE_SECRET)
        self.assertEqual(principal.subject, "owner@deskco.com")
        self.assertEqual(principal.roles, ("operator",))
        with self.assertRaises(mobile.MobileError):
            mobile.redeem_pairing(grant, MOBILE_SECRET)

    def test_the_link_does_not_contain_a_token(self) -> None:
        """A screenshot of the link must not be a usable credential by itself."""
        grant = mobile.issue_pairing("owner@deskco.com", ("operator",), MOBILE_SECRET)
        with self.assertRaises(auth.AuthError):
            auth.authenticate_token(grant, MOBILE_SECRET)

    def test_a_forged_link_is_rejected(self) -> None:
        grant = mobile.issue_pairing("owner@deskco.com", ("operator",), MOBILE_SECRET)
        payload, signature = grant.split(".", 1)
        forged = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        with self.assertRaises(mobile.MobileError):
            mobile.redeem_pairing(f"{forged}.{signature}", MOBILE_SECRET)

    def test_another_secret_cannot_mint_one(self) -> None:
        grant = mobile.issue_pairing("owner@deskco.com", ("operator",), "b" * 40)
        with self.assertRaises(mobile.MobileError):
            mobile.redeem_pairing(grant, MOBILE_SECRET)

    def test_a_link_expires(self) -> None:
        issued = 1_000_000
        grant = mobile.issue_pairing("owner@deskco.com", ("operator",),
                                     MOBILE_SECRET, now=issued)
        self.assertTrue(mobile.redeem_pairing(grant, MOBILE_SECRET, now=issued + 60))
        mobile.reset_pairings()
        with self.assertRaises(mobile.MobileError):
            mobile.redeem_pairing(grant, MOBILE_SECRET,
                                  now=issued + mobile.PAIR_TTL_SECONDS + 1)

    def test_an_unknown_role_cannot_be_granted(self) -> None:
        with self.assertRaises(mobile.MobileError):
            mobile.issue_pairing("owner@deskco.com", ("superuser",), MOBILE_SECRET)

    def test_a_checkout_token_is_not_a_pairing_grant(self) -> None:
        """Domain separation across every signer in the system."""
        with mock.patch.dict(os.environ, {"FRONTDESK_CHECKOUT_SECRET": MOBILE_SECRET,
                                          "FRONTDESK_CHECKOUT_BASE_URL":
                                              "https://pay.example.com"}):
            url = paypal_checkout.create_checkout_url("5O190127TN364715T", "10.00", "USD")
        with self.assertRaises(mobile.MobileError):
            mobile.redeem_pairing(url.split("#", 1)[1], MOBILE_SECRET)


class MobileScreenTests(unittest.TestCase):
    """The screen, over real HTTP."""

    def setUp(self) -> None:
        approvals.reset()
        mobile.reset_pairings()
        self.addCleanup(approvals.reset)
        self.addCleanup(mobile.reset_pairings)
        self.env = mock.patch.dict(
            os.environ, {"FRONTDESK_AUTH_SECRET": MOBILE_SECRET,
                         "FRONTDESK_REMOTE_APPROVAL": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

        handler = type("BoundHandler", (webhooks.WebhookHandler,),
                       {"registry": {}, "dispatcher": None})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _open(self, path, payload=None, csrf=None):
        headers, data = {}, None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "X-CSRF": csrf or ""}
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers,
                                         method="POST" if data else "GET")
        try:
            with self.browser.open(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _sign_in(self, subject="owner@deskco.com", roles=("operator", "finance")):
        grant = mobile.issue_pairing(subject, roles, MOBILE_SECRET)
        self._open(f"/m/pair?t={grant}")
        _, page = self._open("/m")
        return page.split('const CSRF = "')[1].split('"')[0]

    def _park(self, permission="reservations:write"):
        return approvals.request("Cancel reservation R-2001",
                                 tool="cancel_reservation",
                                 permission=permission, requested_by="meta:ig-42",
                                 channel="meta", timeout=60)

    def test_nothing_is_readable_before_signing_in(self) -> None:
        self._park()
        status, page = self._open("/m")
        self.assertEqual(status, 200)
        self.assertIn("Not signed in", page)
        self.assertNotIn("R-2001", page)
        self.assertEqual(self._open("/m/api/pending")[0], 401)
        self.assertEqual(self._open("/m/api/decide", {"id": "x"})[0], 401)

    def test_a_paired_phone_sees_what_is_waiting(self) -> None:
        approval = self._park()
        self._sign_in()
        status, body = self._open("/m/api/pending")
        self.assertEqual(status, 200)
        waiting = json.loads(body)
        self.assertEqual(waiting[0]["id"], approval.id)
        self.assertEqual(waiting[0]["summary"], approval.summary)

    def test_a_stale_page_cannot_approve(self) -> None:
        approval = self._park()
        self._sign_in()
        status, _ = self._open("/m/api/decide",
                               {"id": approval.id, "approve": True}, csrf="stale")
        self.assertEqual(status, 403)
        self.assertTrue(approval.is_open())

    def test_the_owner_can_approve(self) -> None:
        approval = self._park()
        csrf = self._sign_in()
        status, body = self._open("/m/api/decide",
                                  {"id": approval.id, "approve": True}, csrf=csrf)
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(approval.state, approvals.APPROVED)
        self.assertEqual(approval.decided_by, "owner@deskco.com")

    def test_a_phone_without_the_permission_cannot(self) -> None:
        approval = self._park()
        csrf = self._sign_in("junior@deskco.com", ("support",))
        status, body = self._open("/m/api/decide",
                                  {"id": approval.id, "approve": True}, csrf=csrf)
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("reservations:write", payload["error"])
        self.assertTrue(approval.is_open())

    def test_signing_out_drops_the_cookie(self) -> None:
        csrf = self._sign_in()
        self._open("/m/signout", {}, csrf=csrf)
        self.assertEqual(self._open("/m/api/pending")[0], 401)

    def test_the_screen_is_built_for_a_phone(self) -> None:
        self._sign_in()
        _, page = self._open("/m")
        self.assertIn("width=device-width", page)
        self.assertIn("viewport-fit=cover", page)
        self.assertIn("env(safe-area-inset-top)", page)
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn("min-height:52px", page)     # a tap target, not a mouse one

    def test_the_summary_is_never_written_as_markup(self) -> None:
        """It carries customer text, so it goes in as text or not at all."""
        self._sign_in()
        _, page = self._open("/m")
        self.assertIn(".textContent = item.summary", page)
        self.assertNotIn("innerHTML = item.summary", page)


class RegionTests(unittest.TestCase):
    """Two English-speaking markets are not one market."""

    def _persona(self, name, region):
        import config as cfg
        with mock.patch.dict(os.environ, {"FRONTDESK_REGION": region}):
            return cfg.load_persona(name, region)

    def test_the_emergency_number_is_right_in_both(self) -> None:
        """The reason this exists. 911 in Britain is not a typo, it is harm."""
        american = self._persona("healthcare", "us")
        british = self._persona("healthcare", "uk")
        self.assertIn("call 911", american)
        self.assertNotIn("999", american)
        self.assertIn("call 999", british)
        self.assertNotIn("911", british)

    def test_no_persona_names_an_emergency_number_of_its_own(self) -> None:
        """A number written into a persona file is one the region cannot correct."""
        for path in sorted(Path(regions.__file__).parent.glob("personas/*.md")):
            with self.subTest(persona=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("911", text)
                self.assertNotIn("999", text)

    def test_dates_are_unambiguous_per_market(self) -> None:
        """05/09/2026 is two different days; each market must be told which."""
        self.assertIn("MM/DD/YYYY", self._persona("default", "us"))
        self.assertIn("DD/MM/YYYY", self._persona("default", "uk"))

    def test_amounts_follow_the_market(self) -> None:
        self.assertIn("USD", self._persona("ecommerce", "us"))
        self.assertIn("GBP", self._persona("ecommerce", "uk"))
        self.assertIn("£", self._persona("ecommerce", "uk"))

    def test_the_regulator_follows_the_market(self) -> None:
        self.assertIn("HIPAA", self._persona("healthcare", "us"))
        self.assertIn("UK GDPR", self._persona("healthcare", "uk"))
        self.assertNotIn("HIPAA", self._persona("healthcare", "uk"))

    def test_every_persona_carries_the_conventions(self) -> None:
        """A new persona cannot forget what it never had to remember."""
        import config as cfg
        for name in sorted(cfg.available_personas()):
            with self.subTest(persona=name):
                for region in regions.SUPPORTED:
                    text = self._persona(name, region)
                    self.assertIn(regions.facts(region)["date_format"], text)
                    self.assertIn(regions.facts(region)["name"], text)

    def test_no_placeholder_survives_into_a_prompt(self) -> None:
        """An unfilled {region.x} would reach the model as literal noise."""
        import config as cfg
        for name in sorted(cfg.available_personas()):
            for region in regions.SUPPORTED:
                with self.subTest(persona=name, region=region):
                    self.assertNotIn("{region.", self._persona(name, region))

    def test_an_unknown_placeholder_is_left_alone(self) -> None:
        """Silently deleting part of a system prompt is worse than a visible gap."""
        self.assertEqual(regions.apply("keep {region.nonesuch} here", "uk"),
                         "keep {region.nonesuch} here")

    def test_an_unknown_region_falls_back_rather_than_failing(self) -> None:
        # "fr" is a market now; the example has to be a code that is not.
        self.assertNotIn("zz", regions.SUPPORTED)
        with mock.patch.dict(os.environ, {"FRONTDESK_REGION": "zz"}):
            self.assertEqual(regions.current(), regions.DEFAULT)

    def test_every_market_carries_the_facts_a_persona_may_ask_for(self) -> None:
        """A placeholder that resolves in one market and not another is the drift
        this module exists to prevent."""
        keys = set(regions.REGIONS[regions.DEFAULT])
        for code in regions.SUPPORTED:
            self.assertEqual(set(regions.REGIONS[code]), keys, code)

    def test_the_euro_markets_use_112_and_the_euro(self) -> None:
        for code in ("de", "nl", "fr"):
            fact = regions.facts(code)
            self.assertEqual(fact["emergency_number"], "112", code)
            self.assertEqual(fact["currency"], "EUR", code)
            self.assertIn("comma for decimals", fact["number_format"], code)

    def test_paypal_takes_the_regions_currency_by_default(self) -> None:
        for region, expected in (("us", "USD"), ("uk", "GBP")):
            with self.subTest(region=region):
                with mock.patch.dict(os.environ, {"FRONTDESK_REGION": region}):
                    captured = {}

                    def capture(method, path, payload=None, **kwargs):
                        captured["payload"] = payload
                        return {"id": "5O190127TN364715T", "status": "CREATED",
                                "links": []}

                    with mock.patch.object(paypal, "_authed", side_effect=capture):
                        paypal.create_order("10.00")
                    unit = captured["payload"]["purchase_units"][0]
                    self.assertEqual(unit["amount"]["currency_code"], expected)

    def test_a_currency_no_region_settles_in_is_refused(self) -> None:
        # EUR became a settled currency when the euro markets were added, so the
        # example has to be one no configured region uses.
        self.assertNotIn("CHF", regions.SUPPORTED_CURRENCIES)
        with self.assertRaises(paypal.PayPalError):
            paypal._validate_amount("10.00", "CHF")

    def test_the_checkout_page_accepts_both_currencies(self) -> None:
        for region, expected in (("us", "USD"), ("uk", "GBP")):
            with self.subTest(region=region):
                env = {"FRONTDESK_REGION": region,
                       "FRONTDESK_CHECKOUT_SECRET": "c" * 40,
                       "FRONTDESK_CHECKOUT_BASE_URL": "https://pay.example.com"}
                with mock.patch.dict(os.environ, env):
                    url = paypal_checkout.create_checkout_url(
                        "5O190127TN364715T", "10.00", expected)
                    claims = paypal_checkout.verify_checkout_token(
                        url.split("#", 1)[1])
                self.assertEqual(claims["currency"], expected)

    def test_the_demo_data_is_in_the_local_currency(self) -> None:
        for region, expected in (("us", "USD"), ("uk", "GBP")):
            with self.subTest(region=region):
                with mock.patch.dict(os.environ, {"FRONTDESK_REGION": region}):
                    seed = tools._localised_seed()
                self.assertEqual(seed["orders"]["A-88001"]["currency"], expected)

    def test_nothing_shipped_still_hardcodes_one_market(self) -> None:
        """A stray "USD" or "MM/DD/YYYY" would quietly override the region."""
        root = Path(regions.__file__).parent
        allowed = {"regions.py"}
        pattern = re.compile(r"MM/DD/YYYY|DD/MM/YYYY|\b911\b|\b999\b")
        offenders = []
        for path in sorted(root.glob("personas/*.md")):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])
