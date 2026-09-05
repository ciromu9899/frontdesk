from __future__ import annotations

import io
import os
from unittest import TestCase, mock

import auth
import quickstart


class QuickStartTests(TestCase):
    def test_runtime_environment_is_salon_scoped_and_token_is_valid(self) -> None:
        environment, token = quickstart.build_runtime_environment(
            {"PRESERVED": "yes"}, provider="echo", region="uk", tenant="salon-a")

        self.assertEqual(environment["PRESERVED"], "yes")
        self.assertEqual(environment["FRONTDESK_INDUSTRY"], "salon")
        self.assertEqual(environment["FRONTDESK_WEB_PERSONA"], "salon")
        self.assertEqual(environment["FRONTDESK_WEB_PROVIDER"], "echo")
        self.assertEqual(environment["FRONTDESK_WEB_TENANT_ID"], "salon-a")
        self.assertEqual(environment["FRONTDESK_REGION"], "uk")
        principal = auth.authenticate_token(token, environment["FRONTDESK_AUTH_SECRET"])
        self.assertEqual((principal.roles, principal.tenant_id), (("admin",), "salon-a"))

    def test_runtime_environment_does_not_change_parent_environment(self) -> None:
        original = dict(os.environ)
        quickstart.build_runtime_environment(
            os.environ, provider="echo", region="us", tenant="salon-a")
        self.assertEqual(dict(os.environ), original)

    def test_ollama_model_check_accepts_expected_model(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(
            b'{"models":[{"name":"qwen3:8b"}]}'
        )
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertTrue(quickstart.ollama_model_available())

    def test_ollama_model_check_rejects_missing_model(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"models":[]}')
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertFalse(quickstart.ollama_model_available())

    def test_ollama_model_check_uses_configured_base_url(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(
            b'{"models":[{"name":"qwen3:8b"}]}'
        )
        with mock.patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://ollama:11434"}), \
                mock.patch("urllib.request.urlopen", return_value=response) as opened:
            self.assertTrue(quickstart.ollama_model_available())
        self.assertEqual(opened.call_args.args[0], "http://ollama:11434/api/tags")
