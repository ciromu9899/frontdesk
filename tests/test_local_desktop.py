from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import config
import local_ai
import providers


class _Response(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}


class LocalAIRuntimeTests(TestCase):
    def test_server_command_is_local_and_openai_compatible(self) -> None:
        command = local_ai.server_command(Path("llama-server.exe"), Path("model.gguf"), 12000)
        self.assertEqual(command[:5], ["llama-server.exe", "-m", "model.gguf", "--host", "127.0.0.1"])
        self.assertIn("--jinja", command)
        self.assertEqual(command[command.index("--alias") + 1], "frontdesk-local")
        self.assertEqual(command[command.index("--reasoning") + 1], "off")
        self.assertEqual(command[command.index("--cors-origins") + 1], "localhost")
        self.assertIn("--no-webui", command)

    def test_model_download_is_atomic_and_hash_verified(self) -> None:
        body = b"small deterministic model fixture"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(local_ai, "MODEL_SHA256", hashlib.sha256(body).hexdigest()):
            destination = Path(directory) / "model.gguf"
            progress = mock.Mock()
            result = local_ai.download_model(
                destination, opener=lambda _url: _Response(body), progress=progress)
            self.assertEqual(result.read_bytes(), body)
            self.assertTrue(local_ai.model_ready(result))
            self.assertFalse(result.with_suffix(".gguf.part").exists())
            progress.assert_called()

    def test_bad_model_hash_is_rejected_without_installing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(local_ai, "MODEL_SHA256", "0" * 64):
            destination = Path(directory) / "model.gguf"
            with self.assertRaisesRegex(local_ai.LocalAIError, "SHA-256"):
                local_ai.download_model(destination, opener=lambda _url: _Response(b"tampered"))
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".gguf.part").exists())

    def test_packaged_data_uses_local_appdata(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}, clear=False), \
                mock.patch.object(config.sys, "frozen", True, create=True):
            self.assertEqual(
                config.application_data_dir(),
                Path(r"C:\Users\Test\AppData\Local") / "ShellieSoftwareTools" / "FrontDesk" / "data",
            )


class LlamaCppProviderTests(TestCase):
    def test_streams_openai_compatible_local_response_without_api_key(self) -> None:
        captured: dict = {}

        def fake_post(url, headers, payload):
            captured.update({"url": url, "headers": headers, "payload": payload})
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]})
            yield "data: [DONE]"

        configuration = config.Config(provider="llamacpp", model="frontdesk-local").resolve()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("providers._post_lines", fake_post):
            chunks = list(providers.LlamaCppProvider(configuration).stream(
                "system", [providers.Turn("user", "Hi")], []))

        self.assertEqual(captured["url"], "http://127.0.0.1:11435/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer local-no-credential")
        self.assertEqual([chunk.text for chunk in chunks if chunk.kind == "text"], ["Hello"])
        self.assertEqual(chunks[-1].kind, "final")
