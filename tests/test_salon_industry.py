from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock

import auth
import admin
import audit
import chat
import config as app_config
import connectors
import handoffs
import integrations
import salon_reminders
import tools


def _open_day() -> str:
    candidate = date.today() + timedelta(days=1)
    while candidate.isoweekday() == 7:
        candidate += timedelta(days=1)
    return candidate.isoformat()


class SalonIndustryPackTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = mock.patch.dict(os.environ, {
            "FRONTDESK_STATE_DB": str(Path(self.temporary.name) / "state.db"),
            "FRONTDESK_INDUSTRY": "salon",
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        tools.reset_store("salon-a")
        self.principal = auth.Principal(
            "linkedin:buyer@example.com", ("support",), "salon-a"
        )

    def _execute(self, name: str, arguments: dict, call_id: str = "call-1"):
        return tools.execute(
            tools.ToolCall(call_id, name, arguments), principal=self.principal,
            context={"tenant_id": "salon-a", "channel": "web", "thread_key": "web:1"},
        )

    def test_pack_is_hidden_and_rejected_when_not_enabled(self) -> None:
        configuration = app_config.Config(provider="echo", use_tools=True).resolve()
        with mock.patch.dict(os.environ, {"FRONTDESK_INDUSTRY": ""}):
            session = chat.Session(configuration, chat.Style(False), self.principal)
            self.assertNotIn("list_salon_services", {tool.name for tool in session.active_tools()})
            result = self._execute("list_salon_services", {})
        self.assertTrue(result.is_error)
        self.assertIn("not enabled", result.content)

    def test_catalog_and_availability_are_tenant_scoped(self) -> None:
        catalog = self._execute("list_salon_services", {})
        self.assertFalse(catalog.is_error, catalog.content)
        payload = json.loads(catalog.content)
        self.assertEqual({item["service_id"] for item in payload["services"]},
                         {"CUT", "BLOWOUT", "COLOR", "PATCH"})
        slots = self._execute("find_salon_appointment_slots", {
            "appointment_date": _open_day(), "service_id": "CUT",
            "staff_id": "STY-1", "location_id": "LOC-1",
        })
        self.assertFalse(slots.is_error, slots.content)
        found = json.loads(slots.content)["slots"]
        self.assertTrue(found)
        self.assertTrue(all(item["staff_id"] == "STY-1" for item in found))

    def test_booking_uses_verified_email_and_prevents_double_booking(self) -> None:
        appointment_date = _open_day()
        slots = json.loads(self._execute("find_salon_appointment_slots", {
            "appointment_date": appointment_date, "service_id": "CUT",
            "staff_id": "STY-1", "location_id": "LOC-1",
        }).content)["slots"]
        arguments = {
            "customer_name": "Buyer", "email": "spoofed@example.com",
            "appointment_date": appointment_date,
            "appointment_time": slots[0]["time"], "service_id": "CUT",
            "staff_id": "STY-1", "location_id": "LOC-1", "send_reminders": True,
        }
        booked = self._execute("book_salon_appointment", arguments, "book-1")
        self.assertFalse(booked.is_error, booked.content)
        reservation = json.loads(booked.content)
        self.assertEqual(reservation["email"], "buyer@example.com")
        self.assertEqual(reservation["reminder_status"], "pending")
        duplicate = self._execute("book_salon_appointment", arguments, "book-2")
        self.assertTrue(duplicate.is_error)
        self.assertIn("no longer available", duplicate.content)
        self.assertTrue(tools.REGISTRY["book_salon_appointment"].dangerous)

    def test_colour_and_safety_requests_require_human_review(self) -> None:
        appointment_date = _open_day()
        slots = json.loads(self._execute("find_salon_appointment_slots", {
            "appointment_date": appointment_date, "service_id": "COLOR",
            "staff_id": "STY-1", "location_id": "LOC-1",
        }).content)["slots"]
        blocked = self._execute("book_salon_appointment", {
            "customer_name": "Buyer", "email": "buyer@example.com",
            "appointment_date": appointment_date, "appointment_time": slots[0]["time"],
            "service_id": "COLOR", "staff_id": "STY-1", "location_id": "LOC-1",
            "consultation_confirmed": False,
        })
        self.assertTrue(blocked.is_error)
        self.assertIn("consultation", blocked.content)

        configuration = app_config.Config(provider="echo", use_tools=True).resolve()
        session = chat.Session(configuration, chat.Style(False), self.principal)
        with mock.patch.object(session, "_invoke", return_value=tools.ToolResult(
                "policy-1", "request_human_handoff",
                json.dumps({"handoff_id": "H-SALON"}))) as invoke, \
                mock.patch.object(session, "_one_exchange") as model:
            reply = session.ask("I had an allergic reaction. Is hair colour safe for me?")
        invoke.assert_called_once()
        model.assert_not_called()
        self.assertIn("cannot assess", reply)
        self.assertIn("H-SALON", reply)

    def test_admin_dashboard_and_api_show_salon_appointments(self) -> None:
        store = tools.load_store("salon-a")
        store["reservations"]["R-MOBILE"] = {
            "customer": "Buyer", "email": "buyer@example.com",
            "date": _open_day(), "time": "10:00", "timezone": "America/New_York",
            "status": "confirmed", "service_id": "CUT", "service": "Haircut",
            "staff_id": "STY-1", "staff": "Alex Rivera", "location_id": "LOC-1",
            "location": "Downtown Studio", "reminder_status": "pending",
        }
        tools.save_store(store, "salon-a")
        secret = "salon-admin-secret-that-is-longer-than-thirty-two-characters"
        token = auth.issue_token(auth.Principal("owner@example.com", ("admin",), "salon-a"),
                                 secret, 60)
        server = ThreadingHTTPServer(("127.0.0.1", 0), admin.AdminHandler)
        server.auth_secret = secret  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with mock.patch.object(audit, "AUDIT_PATH", Path(self.temporary.name) / "audit.jsonl"), \
                    mock.patch.object(handoffs, "HANDOFF_PATH", Path(self.temporary.name) / "handoffs.jsonl"):
                login = urllib.request.Request(
                    base + "/login",
                    data=urllib.parse.urlencode({"token": token}).encode(), method="POST")
                dashboard = opener.open(login, timeout=2).read().decode()
                self.assertIn("Upcoming salon appointments", dashboard)
                self.assertIn("R-MOBILE", json.dumps(json.loads(
                    opener.open(base + "/api/appointments", timeout=2).read())))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


class SalonBackendContractTests(TestCase):
    def test_external_backend_paths_and_idempotency(self) -> None:
        backend = connectors.RestBackend(
            connectors.BackendConfig("https://booking.example", "token"), "salon-a"
        )
        with mock.patch.object(backend, "request", return_value={"ok": True}) as request:
            backend.list_services()
            backend.find_appointment_slots("CUT", "2026-09-01",
                                           staff_id="STY-1", location_id="LOC-1")
            backend.create_appointment({"service_id": "CUT"}, "REQ-1")
        self.assertEqual(request.call_args_list[0].args, ("GET", "/services"))
        self.assertEqual(request.call_args_list[1].args, ("GET", "/availability"))
        self.assertEqual(request.call_args_list[1].kwargs["query"]["staff_id"], "STY-1")
        self.assertEqual(request.call_args_list[2].args, ("POST", "/appointments"))
        self.assertEqual(request.call_args_list[2].kwargs["idempotency_key"], "REQ-1")


class SalonReminderTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = mock.patch.dict(os.environ, {
            "FRONTDESK_STATE_DB": str(Path(self.temporary.name) / "state.db"),
            "FRONTDESK_INDUSTRY": "salon",
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        start = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
        store = tools.load_store("salon-reminders")
        store["reservations"]["R-REMIND"] = {
            "customer": "Buyer", "email": "buyer@example.com",
            "date": "2030-01-02", "time": "12:00", "timezone": "UTC",
            "status": "confirmed", "service": "Haircut", "staff": "Alex",
            "location": "Downtown", "reminders": True, "reminder_status": "pending",
        }
        tools.save_store(store, "salon-reminders")
        self.now = start - timedelta(hours=2)

    def test_success_is_sent_once_and_failure_remains_retryable(self) -> None:
        with mock.patch("salon_reminders.integrations.send_email",
                        side_effect=integrations.IntegrationError("outage")):
            failed = salon_reminders.send_due(
                "salon-reminders", now=self.now, within_hours=3)
        self.assertEqual(failed, {"due": 1, "sent": 0, "failed": 1, "dry_run": False})
        self.assertEqual(tools.load_store("salon-reminders")["reservations"]
                         ["R-REMIND"]["reminder_status"], "pending")

        with mock.patch("salon_reminders.integrations.send_email") as send:
            sent = salon_reminders.send_due(
                "salon-reminders", now=self.now, within_hours=3)
            repeated = salon_reminders.send_due(
                "salon-reminders", now=self.now, within_hours=3)
        self.assertEqual(sent["sent"], 1)
        self.assertEqual(repeated["due"], 0)
        send.assert_called_once()

    def test_dry_run_never_sends_or_marks_complete(self) -> None:
        with mock.patch("salon_reminders.integrations.send_email") as send:
            result = salon_reminders.send_due(
                "salon-reminders", now=self.now, within_hours=3, dry_run=True)
        self.assertEqual(result, {"due": 1, "sent": 0, "failed": 0, "dry_run": True})
        send.assert_not_called()
        self.assertEqual(tools.load_store("salon-reminders")["reservations"]
                         ["R-REMIND"]["reminder_status"], "pending")


class UnsupportedPriceTests(TestCase):
    """A price the tools never returned must not reach a customer.

    Measured before this guard, with the bundled qwen3:8b model: three Dutch
    replies out of three quoted 15,00 EUR for a service the catalogue prices at
    75,00. Prompt wording did not stop it, so the check compares what was said
    against what the tools actually returned.
    """

    def _session(self, lang="en"):
        configuration = app_config.Config(provider="echo", persona="salon", ui_lang=lang,
                                   use_tools=True).resolve()
        return chat.Session(configuration, chat.Style(False),
                            auth.Principal("p", ("operator",), "pricetest"),
                            out=io.StringIO(),
                            context={"tenant_id": "pricetest", "channel": "web"})

    def test_money_is_read_the_same_in_every_market(self):
        for text, expected in (
            ("75.00 USD", 75.0), ("75,00 EUR", 75.0), ("15,00 EUR.", 15.0),
            ("1.234,50 EUR", 1234.5), ("1 234,50 EUR", 1234.5),
        ):
            self.assertIn(expected, chat._amounts(text), text)

    def test_a_number_that_is_not_money_is_left_alone(self):
        """Times, dates and counts must not be able to trigger a refusal."""
        for text in ("60 minutes", "le 5 septembre", "2 personen",
                     "op 05-09-2026 om 14:30", "service ID CUT"):
            self.assertEqual(chat._amounts(text), set(), text)

    def test_a_trailing_separator_does_not_change_the_amount(self):
        """"75.0," in JSON once read as 750, which would have hidden a real price."""
        self.assertEqual(chat._bare_numbers('{"price": 75.0, "b": 55.0}'), {75.0, 55.0})

    def test_a_price_the_tools_returned_is_allowed_through(self):
        session = self._session()
        session._turn_tool_output = ['{"service": "CUT", "price": 75.0}']
        reply = "A haircut costs $75.00 and takes 60 minutes."
        self.assertFalse(session._priced_beyond_evidence(reply))
        self.assertEqual(session._finalize_grounded_reply(reply), reply)

    def test_a_price_no_tool_returned_is_withheld(self):
        session = self._session()
        session._turn_tool_output = ['{"service": "CUT", "price": 75.0}']
        withheld = session._finalize_grounded_reply("Een knipbeurt kost 15,00 EUR.")
        self.assertNotIn("15", withheld)
        self.assertIn("confirm", withheld.casefold())

    def test_a_reply_with_no_price_is_untouched(self):
        session = self._session()
        session._turn_tool_output = []
        reply = "We are open from 9am on Saturday."
        self.assertEqual(session._finalize_grounded_reply(reply), reply)

    def test_the_customer_is_told_in_their_own_language(self):
        for lang, marker in (("de", "bestätigen"), ("nl", "bevestigen"),
                             ("fr", "confirmer"), ("es", "confirmar")):
            session = self._session(lang)
            session._turn_tool_output = []
            withheld = session._finalize_grounded_reply("Das kostet 15,00 EUR.")
            self.assertIn(marker, withheld, lang)
