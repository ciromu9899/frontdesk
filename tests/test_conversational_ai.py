import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auth
import chat
import config
import rag
import state
import webchat
from providers import Turn


class ConversationalAITests(unittest.TestCase):
    def session(self) -> chat.Session:
        settings = config.Config(
            provider="echo", persona="ecommerce", use_tools=True
        ).resolve()
        with mock.patch.object(chat.audit, "record"):
            return chat.Session(
                settings,
                chat.Style(False),
                auth.Principal("test-user", ("guest",), "web:test"),
            )

    def test_sensitive_input_is_rejected_without_history(self) -> None:
        session = self.session()
        before = list(session.history)
        with mock.patch.object(chat.audit, "record") as record:
            reply = session.ask("My password is example-secret.")
        self.assertIn("cannot accept or store", reply)
        self.assertEqual(before, session.history)
        record.assert_called_once()
        self.assertEqual(
            "password", record.call_args.kwargs["details"]["category"]
        )

    def test_card_detection_uses_luhn(self) -> None:
        session = self.session()
        self.assertEqual(
            "payment_card", session._sensitive_input_category("4111 1111 1111 1111")
        )
        self.assertEqual("", session._sensitive_input_category("order 4111"))

    def test_low_relevance_knowledge_is_not_attached(self) -> None:
        session = self.session()
        low = rag.SearchHit("irrelevant.md", 1, 0.8, "Unrelated text")
        high = rag.SearchHit("relevant.md", 1, 1.2, "Relevant text")
        with mock.patch.object(rag, "search", return_value=[low]):
            self.assertEqual("", session._grounding_context("What is my name?"))
        with mock.patch.object(rag, "search", return_value=[high]):
            self.assertIn("relevant.md#chunk-1", session._grounding_context("Product?"))

    def test_web_limits_are_bounded(self) -> None:
        with mock.patch.dict(os.environ, {"WEB_LIMIT": "invalid"}):
            self.assertEqual(256, webchat._bounded_env_int("WEB_LIMIT", 256, 64, 2048))
        with mock.patch.dict(os.environ, {"WEB_LIMIT": "99999"}):
            self.assertEqual(2048, webchat._bounded_env_int("WEB_LIMIT", 256, 64, 2048))

    def test_session_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            session = self.session()
            session.session_id = "session-1"
            session.history = [
                Turn("user", text="My name is Alex."),
                Turn("assistant", text="Hello, Alex."),
            ]
            payload = session.durable_payload()
            state.save_session("web:test", "web:conversation", payload, database)
            restored = state.load_session("web:test", "web:conversation", database)
            self.assertIsNotNone(restored)
            history = chat.Session.deserialize_history(restored["history"])
            self.assertEqual("My name is Alex.", history[0].text)


if __name__ == "__main__":
    unittest.main()
