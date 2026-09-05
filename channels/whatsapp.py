"""WhatsApp Cloud API adapter with Meta signature verification."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

import resilience

from channels.base import PUBLIC, ChannelError, InboundMessage
from channels.signatures import meta_verify_challenge, verify_meta

DEFAULT_GRAPH_VERSION = "v26.0"


def graph_base() -> str:
    version = os.environ.get("FRONTDESK_META_GRAPH_VERSION", DEFAULT_GRAPH_VERSION)
    if not re.fullmatch(r"v\d{1,2}\.\d", version):
        raise ChannelError("FRONTDESK_META_GRAPH_VERSION is invalid.")
    return f"https://graph.facebook.com/{version}"


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self) -> None:
        self.app_secret = os.environ.get("FRONTDESK_WHATSAPP_APP_SECRET", "")
        self.token = os.environ.get("FRONTDESK_WHATSAPP_TOKEN", "")
        self.verify_token = os.environ.get("FRONTDESK_WHATSAPP_VERIFY_TOKEN", "")
        self._circuit = resilience.CircuitBreaker()

    def configured(self) -> bool:
        return bool(self.app_secret and self.token)

    def verify(self, headers: dict, body: bytes) -> bool:
        return verify_meta(headers, body, self.app_secret)

    def challenge(self, query: dict) -> str | None:
        return meta_verify_challenge(query, self.verify_token)

    def parse(self, body: bytes) -> list[InboundMessage]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ChannelError("WhatsApp payload was not valid JSON.") from None
        messages: list[InboundMessage] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}; metadata = value.get("metadata") or {}
                phone_id = str(metadata.get("phone_number_id", ""))
                for item in value.get("messages", []):
                    sender = str(item.get("from", "")); text = str((item.get("text") or {}).get("body", "")).strip()
                    if not sender or not text or not phone_id:
                        continue
                    messages.append(InboundMessage(
                        self.name, sender, f"{phone_id}:{sender}", text, PUBLIC,
                        raw=item, tenant_id=f"whatsapp:{phone_id}"))
        return messages

    def send(self, thread_key: str, text: str) -> None:
        try:
            phone_id, recipient = thread_key.split(":", 1)
        except ValueError:
            raise ChannelError("WhatsApp thread key is invalid.") from None
        payload = {"messaging_product": "whatsapp", "to": recipient, "type": "text",
                   "text": {"body": text[:4096]}}
        request = urllib.request.Request(
            f"{graph_base()}/{phone_id}/messages", data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST")
        def perform() -> None:
            with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
                response.read()

        try:
            resilience.execute(perform, retry_safe=False, breaker=self._circuit)
        except resilience.CircuitOpenError:
            raise ChannelError("WhatsApp circuit is open after repeated failures.") from None
        except urllib.error.HTTPError as exc:
            raise ChannelError(f"WhatsApp returned HTTP {exc.code}.") from None
        except urllib.error.URLError as exc:
            raise ChannelError(f"Could not reach WhatsApp: {exc.reason}") from None
