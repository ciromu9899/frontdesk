from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock

import handoffs
import integrations
import auth
import state
import tools
import webchat
import webhooks
from channels.email import EmailChannel
from channels.whatsapp import WhatsAppChannel


class SharedInboxTests(TestCase):
    def test_threads_messages_csat_and_tenants_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            state.append_message("a", "web:1", "customer", "hello", database=database)
            state.append_message("a", "web:1", "assistant", "hi", database=database)
            state.record_csat("a", "web:1", 5, database=database)
            self.assertEqual(len(state.list_messages("a", "web:1", database=database)), 2)
            self.assertEqual(state.analytics("a", database=database)["csat_average"], 5.0)
            self.assertEqual(state.list_threads("b", database=database), [])

    def test_privacy_export_and_delete_include_inbox_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            state.append_message("a", "c1", "customer", "private", sender_id="buyer", database=database)
            state.append_message("a", "c1", "assistant", "reply", database=database)
            state.record_csat("a", "c1", 3, database=database)
            exported = state.export_subject("a", "buyer", database)
            self.assertEqual((len(exported["threads"]), len(exported["messages"]), len(exported["csat"])), (1, 2, 1))
            removed = state.delete_subject("a", "buyer", database)
            self.assertEqual((removed["threads"], removed["messages"], removed["csat"]), (1, 2, 1))
            self.assertEqual(state.list_threads("a", database=database), [])

    def test_handoff_can_be_taken_noted_and_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "handoffs.jsonl"
            ticket = handoffs.request("Need help", requested_by="buyer", tenant_id="a", path=path)
            self.assertTrue(handoffs.update(ticket["id"], "started", actor="agent", tenant_id="a", path=path))
            self.assertTrue(handoffs.update(ticket["id"], "note", actor="agent", tenant_id="a", note="Called customer", path=path))
            current = handoffs.list_tickets(status=None, tenant_id="a", path=path)[0]
            self.assertEqual(current["status"], "in_progress")
            self.assertEqual(current["notes"][0]["note"], "Called customer")

    def test_social_handoff_updates_the_same_inbox_thread(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
                "FRONTDESK_STATE_DB": str(Path(temporary) / "state.db")}), \
                mock.patch.object(handoffs, "HANDOFF_PATH", Path(temporary) / "handoffs.jsonl"):
            ticket = handoffs.request("Need help", requested_by="slack:U1", tenant_id="slack:T1",
                                      channel="slack", thread_key="C1:100")
            self.assertTrue(handoffs.update(ticket["id"], "started", actor="agent",
                                            tenant_id="slack:T1"))
            thread = state.get_thread("slack:T1", "slack:C1:100")
            self.assertEqual((thread["status"], thread["assignee"]), ("in_progress", "agent"))


class WidgetTests(TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webchat.WebChatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_embed_script_and_frame_are_served(self):
        script = urllib.request.urlopen(self.base + "/embed.js").read().decode()
        self.assertIn("document.createElement('iframe')", script)
        response = urllib.request.urlopen(self.base + "/widget?lang=es")
        page = response.read().decode()
        self.assertIn("Hablar con una persona", page)
        self.assertIsNone(response.headers.get("X-Frame-Options"))

    def test_embed_origins_reject_header_injection_and_plain_http(self):
        with mock.patch.dict(os.environ, {"FRONTDESK_EMBED_ORIGINS":
                                          "https://shop.example.com http://bad.example X-Test:\\ninjected"}):
            self.assertEqual(webchat._embed_ancestors(), "https://shop.example.com")

    def test_chat_handoff_and_csat_persist(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
                "FRONTDESK_STATE_DB": str(Path(temporary) / "state.db"), "FRONTDESK_WEB_PROVIDER": "echo"}), \
                mock.patch.object(handoffs, "HANDOFF_PATH", Path(temporary) / "handoffs.jsonl"):
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            page = opener.open(self.base + "/").read().decode()
            csrf = page.split("const csrf=", 1)[1].split(",lang=", 1)[0].strip('"')
            for path, payload in (("/api/chat", {"message": "hello", "lang": "en"}),
                                  ("/api/handoff", {}), ("/api/csat", {"rating": 4})):
                request = urllib.request.Request(self.base + path, json.dumps(payload).encode(),
                    {"Content-Type": "application/json", "X-CSRF": csrf}, method="POST")
                self.assertIn(opener.open(request).status, (200, 201))
            self.assertEqual(state.analytics("web:default")["csat_average"], 4.0)
            conversation_key = state.list_threads("web:default")[0]["conversation_key"]
            state.append_message("web:default", conversation_key, "agent", "A teammate is here.",
                                 sender_id="agent")
            polled = json.loads(opener.open(self.base + "/api/messages?after=0").read())
            self.assertEqual(polled["messages"][0]["body"], "A teammate is here.")


class NewChannelTests(TestCase):
    def test_whatsapp_signature_parse_and_delivery_key(self):
        body = json.dumps({"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "p1"},
            "messages": [{"id": "m1", "from": "15551234567", "text": {"body": "Hello"}}]}}]}]}).encode()
        with mock.patch.dict(os.environ, {"FRONTDESK_WHATSAPP_APP_SECRET": "secret", "FRONTDESK_WHATSAPP_TOKEN": "token"}):
            channel = WhatsAppChannel()
            signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            self.assertTrue(channel.verify({"X-Hub-Signature-256": signature}, body))
            message = channel.parse(body)[0]
        self.assertEqual((message.tenant_id, message.text), ("whatsapp:p1", "Hello"))
        self.assertEqual(webhooks.delivery_key("whatsapp", {}, body), "whatsapp:m1")

    def test_email_relay_requires_signature_and_preserves_tenant(self):
        body = json.dumps({"tenant_id": "customer-a", "message_id": "mail-1",
                           "from": "buyer@example.com", "subject": "Help", "text": "Please help"}).encode()
        secret = "s" * 32
        with mock.patch.dict(os.environ, {"FRONTDESK_EMAIL_WEBHOOK_SECRET": secret}):
            channel = EmailChannel()
            signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            self.assertTrue(channel.verify({"X-Frontdesk-Signature": signature}, body))
            message = channel.parse(body)[0]
        self.assertEqual(message.tenant_id, "customer-a")
        self.assertTrue(message.thread_key.startswith("customer-a|"))


class BusinessIntegrationTests(TestCase):
    def test_profiles_resolve_separate_tenant_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "integrations.json"
            path.write_text(json.dumps({"tenants": {
                "a": {"hubspot": {"base_url": "https://api.hubapi.com", "token_env": "TOKEN_A"}},
                "b": {"hubspot": {"base_url": "https://api.hubapi.com", "token_env": "TOKEN_B"}}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"FRONTDESK_INTEGRATIONS_FILE": str(path),
                                               "TOKEN_A": "secret-a", "TOKEN_B": "secret-b"}):
                self.assertEqual(integrations.HubSpot("a").token, "secret-a")
                self.assertEqual(integrations.HubSpot("b").token, "secret-b")

    def test_missing_tenant_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "integrations.json"; path.write_text('{"tenants":{}}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"FRONTDESK_INTEGRATIONS_FILE": str(path)}):
                with self.assertRaises(integrations.IntegrationError):
                    integrations.Shopify("missing")

    def test_external_ticket_tool_is_permission_gated_and_uses_verified_email(self):
        call = tools.ToolCall("c1", "create_support_ticket", {
            "system": "zendesk", "subject": "Help", "description": "Broken",
            "requester_email": "attacker@example.com"})
        denied = tools.execute(call, principal=auth.Principal("guest", ("guest",), "a"))
        self.assertTrue(denied.is_error)
        zendesk = mock.Mock(); zendesk.create_ticket.return_value = {"ticket": {"id": 1}}
        with mock.patch("tools.integrations.Zendesk", return_value=zendesk):
            result = tools.execute(call, principal=auth.Principal(
                "linkedin:buyer@example.com", ("support",), "a"), context={"tenant_id": "a"})
        self.assertFalse(result.is_error, result.content)
        zendesk.create_ticket.assert_called_once_with("Help", "Broken", "buyer@example.com", "c1")
        self.assertTrue(tools.REGISTRY["create_support_ticket"].dangerous)

    def test_shopify_tool_filters_an_authenticated_customer(self):
        shopify = mock.Mock(); shopify.find_order.return_value = {"orders": [
            {"id": 1, "email": "buyer@example.com"}, {"id": 2, "email": "other@example.com"}]}
        with mock.patch("tools.integrations.Shopify", return_value=shopify):
            result = tools.execute(tools.ToolCall("c2", "shopify_find_order", {"order_name": "#1001"}),
                principal=auth.Principal("linkedin:buyer@example.com", ("support",), "a"),
                context={"tenant_id": "a"})
        self.assertFalse(result.is_error, result.content)
        self.assertEqual([order["id"] for order in json.loads(result.content)["orders"]], [1])
