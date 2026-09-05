"""Regression coverage for the features recovered from the 1.3.0 archive."""

from __future__ import annotations

import email
import email.policy
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock

import config
import email_mailbox
import feedback
import state
import webchat


SECRET = "abcdefghijklmnopqrstuvwxyz012345"
OUR_ADDRESS = "help@salon.example"


def message(**headers):
    body = headers.pop("body", "What are your opening hours?")
    lines = [f"{key.replace('_', '-')}: {value}" for key, value in headers.items()]
    return email.message_from_string(
        "\r\n".join(lines) + "\r\n\r\n" + body,
        policy=email.policy.default)


class PersonaIntegrationTests(TestCase):
    def test_every_customer_industry_is_available(self):
        """`ecommerce-es` is Spanish ecommerce, not a seventeenth industry.

        Listing translations as industries stops scaling the moment a market
        needs five languages, so `available_personas` reports industries and
        `load_persona` resolves the translation.
        """
        expected = {
            "agent", "automotive", "ecommerce", "education",
            "events", "fintech", "healthcare", "helpdesk", "homeservices",
            "hospitality", "legal", "professional", "realestate", "recruiting",
            "saas-support", "salon",
        }
        self.assertEqual(set(config.available_personas()) - {"default"}, expected)

    def test_a_translated_persona_is_still_loadable_by_name(self):
        self.assertIn("Eres", config.load_persona("ecommerce-es", region="us", lang="es"))

    def test_every_industry_answers_in_every_supported_language(self):
        """A market without its industry's boundaries is a market that cannot be sold to."""
        for industry in config.available_personas():
            for lang in ("en", "es", "de", "nl", "fr"):
                prompt = config.load_persona(industry, region="de", lang=lang)
                self.assertNotIn("{region.", prompt, f"{industry}/{lang}")
                self.assertTrue(prompt.strip(), f"{industry}/{lang}")

    def test_salon_persona_keeps_regulated_safety_boundaries(self):
        salon = config.load_persona("salon")
        self.assertIn("patch test", salon.casefold())
        self.assertIn("medical", salon.casefold())

    def test_web_persona_is_selectable_and_invalid_values_fail_safe(self):
        with mock.patch.dict(os.environ, {"FRONTDESK_WEB_PERSONA": "salon"}):
            self.assertEqual(webchat._web_persona("en"), "salon")
        with mock.patch.dict(os.environ, {"FRONTDESK_WEB_PERSONA": "missing"}):
            self.assertEqual(webchat._web_persona("es"), "ecommerce-es")


class MailboxParsingTests(TestCase):
    def test_automatic_list_bounce_and_own_messages_are_rejected(self):
        automated = [
            message(From="a@b.com", Auto_Submitted="auto-replied"),
            message(From="a@b.com", List_Id="<news.example>"),
            message(From="MAILER-DAEMON@mx.example"),
            message(From=OUR_ADDRESS),
        ]
        self.assertTrue(all(email_mailbox.is_automated(item, OUR_ADDRESS)
                            for item in automated))

    def test_reply_thread_and_body_are_safely_normalised(self):
        inbound = message(
            From="Dana <dana@example.com>", Subject="Booking",
            Message_ID="<child@example.com>", References="<root@example.com>",
            body="Can I move my appointment?\n\nOn Monday, someone wrote:\n> old text")
        normalised = email_mailbox.to_inbound(inbound, "tenant-a")
        self.assertEqual(normalised.thread_key, "root@example.com")
        self.assertIn("move my appointment", normalised.text)
        self.assertNotIn("old text", normalised.text)
        self.assertEqual(normalised.tenant_id, "tenant-a")
        self.assertEqual(normalised.principal().roles, ("guest",))

    def test_reply_uses_from_instead_of_untrusted_reply_to(self):
        captured = {}

        class Server:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def starttls(self): return None
            def login(self, *args): return None
            def send_message(self, outgoing): captured["message"] = outgoing

        inbound = message(From="Dana <dana@example.com>",
                          Reply_To="victim@example.net", Subject="Hours",
                          Message_ID="<m1@example.com>")
        settings = {"address": OUR_ADDRESS, "smtp_host": "smtp.example",
                    "smtp_port": 587, "user": "user", "password": "secret",
                    "starttls": True}
        with mock.patch.object(email_mailbox.smtplib, "SMTP",
                               lambda *args, **kwargs: Server()):
            email_mailbox.send_reply(settings, inbound, "We open at 9am.")
        self.assertEqual(captured["message"]["To"], "dana@example.com")
        self.assertEqual(captured["message"]["Auto-Submitted"], "auto-replied")


class MailboxRetryTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "state.db"
        self.settings = {"address": OUR_ADDRESS, "tenant_id": "email-a"}
        self.inbound = message(From="Dana <dana@example.com>", Subject="Hours",
                               Message_ID="<mail-1@example.com>")
        self.sent = []

    def poll(self, dispatcher, *, send=True):
        with mock.patch.object(email_mailbox, "fetch_unseen",
                               return_value=[self.inbound]), \
                mock.patch.object(email_mailbox, "send_reply",
                                  side_effect=lambda settings, inbound, text:
                                  self.sent.append(text)), \
                mock.patch.object(email_mailbox.audit, "record"), \
                mock.patch.object(state, "path", return_value=self.database):
            return email_mailbox.poll_once(dispatcher, self.settings, send=send)

    def test_success_is_deduplicated_across_polls(self):
        dispatcher = mock.Mock()
        dispatcher.handle.return_value = "We open at 9am."
        first = self.poll(dispatcher)
        second = self.poll(dispatcher)
        self.assertEqual((first["answered"], second["skipped_duplicate"]), (1, 1))
        self.assertEqual(len(self.sent), 1)

    def test_model_failure_is_left_for_retry(self):
        dispatcher = mock.Mock()
        dispatcher.handle.side_effect = [RuntimeError("model down"), "Recovered reply"]
        first = self.poll(dispatcher)
        second = self.poll(dispatcher)
        self.assertEqual((first["failed"], second["answered"]), (1, 1))
        self.assertEqual(self.sent, ["Recovered reply"])

    def test_smtp_failure_is_left_for_retry(self):
        dispatcher = mock.Mock()
        dispatcher.handle.return_value = "Reply"
        with mock.patch.object(email_mailbox, "fetch_unseen", return_value=[self.inbound]), \
                mock.patch.object(email_mailbox, "send_reply",
                                  side_effect=OSError("smtp unavailable")), \
                mock.patch.object(email_mailbox.audit, "record"), \
                mock.patch.object(state, "path", return_value=self.database):
            failed = email_mailbox.poll_once(dispatcher, self.settings)
        recovered = self.poll(dispatcher)
        self.assertEqual((failed["failed"], recovered["answered"]), (1, 1))

    def test_dry_run_does_not_consume_the_message(self):
        dispatcher = mock.Mock()
        dispatcher.handle.return_value = "Reply"
        dry = self.poll(dispatcher, send=False)
        live = self.poll(dispatcher)
        self.assertEqual((dry["dry_run"], live["answered"]), (1, 1))


class SignedFeedbackTests(TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(
            os.environ, {"FRONTDESK_FEEDBACK_SECRET": SECRET})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "state.db"

    def test_token_rejects_tampering_and_expiry(self):
        token = feedback.issue("tenant-a", "email:thread")
        with self.assertRaises(feedback.FeedbackError):
            feedback.verify(token[:-2] + "AA")
        expired = feedback.issue("tenant-a", "email:thread",
                                 now=time.time() - feedback.TTL_SECONDS - 1)
        with self.assertRaises(feedback.FeedbackError):
            feedback.verify(expired)

    def test_repeat_rating_updates_instead_of_inflating_csat(self):
        token = feedback.issue("tenant-a", "email:thread", channel="email")
        with mock.patch.object(state, "path", return_value=self.database):
            first = feedback.submit(token, 5, "Great")
            second = feedback.submit(token, 2, "Needs work")
            report = state.analytics("tenant-a")
        self.assertFalse(first["updated"])
        self.assertTrue(second["updated"])
        self.assertEqual(report["csat_responses"], 1)
        self.assertEqual(report["csat_average"], 2.0)

    def test_feedback_page_accepts_english_and_spanish_links(self):
        with mock.patch.dict(os.environ, {"FRONTDESK_STATE_DB": str(self.database)}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), webchat.WebChatHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                token = feedback.issue("tenant-a", "email:thread", channel="email")
                page = urllib.request.urlopen(
                    base + "/feedback?t=" + urllib.parse.quote(token) + "&lang=es").read().decode()
                self.assertIn("Tu opinión", page)
                form = urllib.parse.urlencode({"t": token, "score": "4",
                                               "comment": "Helpful"}).encode()
                request = urllib.request.Request(base + "/feedback", form, method="POST")
                result = urllib.request.urlopen(request)
                self.assertEqual(result.status, 200)
                self.assertEqual(state.analytics("tenant-a", database=self.database)[
                    "csat_average"], 4.0)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
