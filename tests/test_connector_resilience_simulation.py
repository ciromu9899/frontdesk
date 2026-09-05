from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import smtplib
import socket
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest import TestCase, mock

import channels
import connectors
import integrations
import state
import webhooks
from channels import linkedin
from channels.email import EmailChannel
from channels.meta import MetaChannel
from channels.signatures import verify_teams
from channels.slack import SlackChannel
from channels.teams import TeamsChannel
from channels.whatsapp import WhatsAppChannel


class _Headers:
    def get_content_type(self) -> str:
        return "application/json"


class _Response:
    def __init__(self, payload: dict | bytes | None = None):
        if isinstance(payload, bytes):
            self.payload = payload
        else:
            self.payload = json.dumps(payload or {}).encode("utf-8")
        self.headers = _Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


def _http_error(code: int, body: bytes = b'{"error":"simulated"}'):
    return urllib.error.HTTPError(
        "https://provider.example/api", code, "simulated", {}, io.BytesIO(body)
    )


class SignedWebhookRetrySimulation(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = mock.patch.dict(
            os.environ,
            {"FRONTDESK_STATE_DB": str(Path(self.temporary.name) / "state.db")},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_all_inbound_connectors_reject_tampering(self) -> None:
        slack_secret = "slack-secret"
        slack_body = b'{"event_id":"E-1"}'
        stamp = str(int(time.time()))
        slack_signature = "v0=" + hmac.new(
            slack_secret.encode(), b"v0:" + stamp.encode() + b":" + slack_body,
            hashlib.sha256,
        ).hexdigest()
        with mock.patch.dict(os.environ, {
            "FRONTDESK_SLACK_SIGNING_SECRET": slack_secret,
            "FRONTDESK_SLACK_BOT_TOKEN": "xoxb-test",
        }):
            slack = SlackChannel()
        slack_headers = {
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": slack_signature,
        }
        self.assertTrue(slack.verify(slack_headers, slack_body))
        self.assertFalse(slack.verify(slack_headers, slack_body + b" "))

        meta_secret = "meta-secret"
        meta_body = b'{"entry":[]}'
        meta_signature = "sha256=" + hmac.new(
            meta_secret.encode(), meta_body, hashlib.sha256
        ).hexdigest()
        meta_headers = {"X-Hub-Signature-256": meta_signature}
        with mock.patch.dict(os.environ, {
            "FRONTDESK_META_APP_SECRET": meta_secret,
            "FRONTDESK_META_PAGE_TOKEN": "page-test",
            "FRONTDESK_WHATSAPP_APP_SECRET": meta_secret,
            "FRONTDESK_WHATSAPP_TOKEN": "wa-test",
        }):
            meta = MetaChannel()
            whatsapp = WhatsAppChannel()
        for channel in (meta, whatsapp):
            self.assertTrue(channel.verify(meta_headers, meta_body))
            self.assertFalse(channel.verify(meta_headers, meta_body + b" "))

        email_secret = "e" * 40
        email_body = b'{"message_id":"mail-1"}'
        email_signature = "sha256=" + hmac.new(
            email_secret.encode(), email_body, hashlib.sha256
        ).hexdigest()
        with mock.patch.dict(os.environ, {"FRONTDESK_EMAIL_WEBHOOK_SECRET": email_secret}):
            email = EmailChannel()
        self.assertTrue(email.verify({"X-Frontdesk-Signature": email_signature}, email_body))
        self.assertFalse(email.verify({"X-Frontdesk-Signature": email_signature}, email_body + b" "))

        teams_key = b"teams-test-key"
        teams_secret = base64.b64encode(teams_key).decode("ascii")
        teams_body = b'{"id":"activity-1"}'
        teams_signature = "HMAC " + base64.b64encode(
            hmac.new(teams_key, teams_body, hashlib.sha256).digest()
        ).decode("ascii")
        self.assertTrue(verify_teams(
            {"Authorization": teams_signature}, teams_body, teams_secret
        ))
        self.assertFalse(verify_teams(
            {"Authorization": teams_signature}, teams_body + b" ", teams_secret
        ))

    def test_retries_are_deduplicated_persistently_and_per_tenant(self) -> None:
        payloads = {
            "slack": b'{"team_id":"T1","event_id":"E1"}',
            "teams": b'{"id":"A1","channelData":{"tenant":{"id":"TEN1"}}}',
            "meta": b'{"entry":[{"id":"P1","messaging":[{"message":{"mid":"M1"}}]}]}',
            "whatsapp": b'{"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"PH1"},"messages":[{"id":"W1"}]}}]}]}',
            "email": b'{"tenant_id":"tenant-a","message_id":"MAIL1"}',
        }
        for name, body in payloads.items():
            with self.subTest(connector=name):
                key = webhooks.delivery_key(name, {}, body)
                tenant = webhooks.delivery_tenant(name, body)
                self.assertTrue(key)
                self.assertFalse(webhooks.already_handled(key, tenant_id=tenant))
                # This second connection represents a provider retry after restart.
                self.assertTrue(webhooks.already_handled(key, tenant_id=tenant))
                self.assertFalse(
                    webhooks.already_handled(key, tenant_id=tenant + ":other"),
                    "one tenant suppressed another tenant's event",
                )

    def test_reply_failure_is_audited_but_provider_retry_is_suppressed(self) -> None:
        class Dispatcher:
            def handle(self, _message):
                return "reply"

        class FailingChannel:
            def send(self, _thread_key, _reply):
                raise channels.ChannelError("simulated send outage")

        handler = webhooks.WebhookHandler.__new__(webhooks.WebhookHandler)
        handler.dispatcher = Dispatcher()
        message = channels.InboundMessage(
            "slack", "U1", "C1:1.0", "hello", trust=channels.WORKSPACE,
            tenant_id="slack:T1",
        )
        body = b'{"team_id":"T1","event_id":"E-send-failed"}'
        key = webhooks.delivery_key("slack", {}, body)
        tenant = webhooks.delivery_tenant("slack", body)
        self.assertFalse(webhooks.already_handled(key, tenant_id=tenant))
        with mock.patch("webhooks.audit.record") as audit_record:
            handler._answer(FailingChannel(), message)
        audit_record.assert_called_once()
        self.assertEqual(audit_record.call_args.args[0], "channel.send_failed")
        # This is intentional characterization of the current residual risk:
        # the platform retry cannot reconstruct the failed outbound response.
        self.assertTrue(webhooks.already_handled(key, tenant_id=tenant))


class SocialOutboundFailureSimulation(TestCase):
    def test_slack_timeout_is_visible_and_a_manual_retry_can_recover(self) -> None:
        with mock.patch.dict(os.environ, {"FRONTDESK_SLACK_BOT_TOKEN": "xoxb-test"}):
            channel = SlackChannel()
        effects = [urllib.error.URLError(socket.timeout("simulated timeout")),
                   _Response({"ok": True})]
        with mock.patch("channels.slack.urllib.request.urlopen", side_effect=effects) as request:
            with self.assertRaisesRegex(channels.ChannelError, "Could not reach Slack"):
                channel.send("C1:1.0", "hello")
            self.assertEqual(request.call_count, 1, "an unsafe hidden retry occurred")
            channel.send("C1:1.0", "hello")
            self.assertEqual(request.call_count, 2)

    def test_meta_500_and_whatsapp_429_are_not_reported_as_success(self) -> None:
        with mock.patch.dict(os.environ, {
            "FRONTDESK_META_PAGE_TOKEN": "page-test",
            "FRONTDESK_WHATSAPP_TOKEN": "wa-test",
        }):
            meta = MetaChannel()
            whatsapp = WhatsAppChannel()
        with mock.patch("channels.meta.urllib.request.urlopen", side_effect=_http_error(500)):
            with self.assertRaisesRegex(channels.ChannelError, r"\(500\)"):
                meta.send("user-1", "hello")
        with mock.patch("channels.whatsapp.urllib.request.urlopen", side_effect=_http_error(429)):
            with self.assertRaisesRegex(channels.ChannelError, "HTTP 429"):
                whatsapp.send("phone-1:user-1", "hello")

    def test_teams_refuses_an_impossible_out_of_band_retry(self) -> None:
        with self.assertRaisesRegex(channels.ChannelError, "HTTP response"):
            TeamsChannel().send("thread-1", "hello")


class BusinessConnectorFailureSimulation(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = Path(self.temporary.name) / "integrations.json"
        self.profile.write_text(json.dumps({"tenants": {"tenant-a": {
            "shopify": {"base_url": "https://shop.example", "token_env": "SHOP_TOKEN"},
            "zendesk": {"base_url": "https://help.example", "token_env": "ZEN_TOKEN"},
            "hubspot": {"base_url": "https://crm.example", "token_env": "HUB_TOKEN"},
            "email": {"smtp_host": "smtp.example", "smtp_port": 465,
                      "username_env": "SMTP_USER", "password_env": "SMTP_PASSWORD",
                      "from": "support@example.com"},
        }}}), encoding="utf-8")
        self.environment = mock.patch.dict(os.environ, {
            "FRONTDESK_INTEGRATIONS_FILE": str(self.profile),
            "SHOP_TOKEN": "shop-secret", "ZEN_TOKEN": "zen-secret",
            "HUB_TOKEN": "hub-secret", "SMTP_USER": "support@example.com",
            "SMTP_PASSWORD": "smtp-secret",
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_safe_reads_retry_transient_failures_then_fail_closed(self) -> None:
        cases = [
            (integrations.Shopify("tenant-a"), _http_error(429), "HTTP 429"),
            (integrations.Zendesk("tenant-a"), _http_error(500), "HTTP 500"),
            (integrations.HubSpot("tenant-a"),
             urllib.error.URLError(socket.timeout("simulated timeout")), "Could not reach"),
        ]
        for client, failure, message in cases:
            with self.subTest(connector=client.name):
                client._opener.open = mock.Mock(side_effect=failure)
                with mock.patch("resilience.time.sleep"):
                    with self.assertRaisesRegex(integrations.IntegrationError, message):
                        client.request("GET", "/simulation")
                self.assertEqual(client._opener.open.call_count, 3,
                                 "the bounded retry policy was not applied")

    def test_zendesk_and_hubspot_preserve_idempotency_key_on_retry(self) -> None:
        for client, invoke in (
            (integrations.Zendesk("tenant-a"),
             lambda item: item.create_ticket("subject", "body", "buyer@example.com", "REQ-1")),
            (integrations.HubSpot("tenant-a"),
             lambda item: item.create_ticket("subject", "body", "REQ-1")),
        ):
            with self.subTest(connector=client.name):
                seen = []

                def open_request(request, timeout):
                    seen.append((request.get_header("Idempotency-key"), timeout))
                    if len(seen) == 1:
                        raise _http_error(503)
                    return _Response({"id": "created"})

                client._opener.open = mock.Mock(side_effect=open_request)
                with mock.patch("resilience.time.sleep"):
                    result = invoke(client)
                self.assertEqual(result["id"], "created")
                self.assertEqual([item[0] for item in seen], ["REQ-1", "REQ-1"])

    def test_smtp_failure_is_visible_and_a_retry_can_recover(self) -> None:
        class SuccessfulSmtp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def login(self, _username, _password):
                return None

            def send_message(self, _message):
                return None

        effects = [smtplib.SMTPException("simulated outage"), SuccessfulSmtp()]
        with mock.patch("integrations.smtplib.SMTP_SSL", side_effect=effects) as smtp:
            with self.assertRaisesRegex(integrations.IntegrationError, "Email delivery failed"):
                integrations.send_email("tenant-a", "buyer@example.com", "subject", "body")
            integrations.send_email("tenant-a", "buyer@example.com", "subject", "body")
            self.assertEqual(smtp.call_count, 2)

    def test_tenant_tokens_never_cross_connector_requests(self) -> None:
        clients = [integrations.Shopify("tenant-a"), integrations.Zendesk("tenant-a"),
                   integrations.HubSpot("tenant-a")]
        expected = ["shop-secret", "zen-secret", "hub-secret"]
        for client, token in zip(clients, expected):
            with self.subTest(connector=client.name):
                seen = {}

                def open_request(request, timeout):
                    seen["authorization"] = request.get_header("Authorization")
                    seen["tenant"] = request.get_header("X-frontdesk-tenant-id")
                    return _Response({})

                client._opener.open = mock.Mock(side_effect=open_request)
                client.request("GET", "/simulation")
                self.assertEqual(seen["authorization"], f"Bearer {token}")
                self.assertEqual(seen["tenant"], "tenant-a")


class LinkedInAndBackendFailureSimulation(TestCase):
    def test_linkedin_429_and_timeout_are_not_treated_as_identity(self) -> None:
        for failure, message in (
            (_http_error(429), "HTTP 429"),
            (urllib.error.URLError(socket.timeout("simulated timeout")), "Could not reach"),
        ):
            with self.subTest(failure=message):
                opener = mock.Mock()
                opener.open.side_effect = failure
                with mock.patch("channels.linkedin.urllib.request.build_opener", return_value=opener):
                    with self.assertRaisesRegex(linkedin.LinkedInError, message):
                        linkedin._read_json(mock.Mock())

    def test_generic_backend_503_and_timeout_fail_closed(self) -> None:
        backend = connectors.RestBackend(connectors.BackendConfig(
            "https://backend.example", "backend-secret"), tenant_id="tenant-a"
        )
        for failure, message in (
            (_http_error(503), "HTTP 503"),
            (urllib.error.URLError(socket.timeout("simulated timeout")), "request failed"),
        ):
            with self.subTest(failure=message):
                backend._opener.open = mock.Mock(side_effect=failure)
                with self.assertRaisesRegex(connectors.ConnectorError, message):
                    backend.change_reservation("R-1", {"date": "2026-09-01"}, "REQ-1")
                request = backend._opener.open.call_args.args[0]
                self.assertEqual(request.get_header("Idempotency-key"), "REQ-1")
