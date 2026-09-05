"""Signed inbound email relay and SMTP outbound adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import integrations
from channels.base import PUBLIC, ChannelError, InboundMessage


class EmailChannel:
    name = "email"

    def __init__(self) -> None:
        self.secret = os.environ.get("FRONTDESK_EMAIL_WEBHOOK_SECRET", "")

    def configured(self) -> bool:
        return len(self.secret) >= 32 and bool(os.environ.get("FRONTDESK_INTEGRATIONS_FILE"))

    def verify(self, headers: dict, body: bytes) -> bool:
        supplied = ""
        for key, value in headers.items():
            if key.lower() == "x-frontdesk-signature": supplied = str(value)
        expected = "sha256=" + hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return len(self.secret) >= 32 and hmac.compare_digest(supplied, expected)

    def parse(self, body: bytes) -> list[InboundMessage]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ChannelError("Email relay payload was not valid JSON.") from None
        sender = str(payload.get("from", "")).strip(); text = str(payload.get("text", "")).strip()
        tenant_id = str(payload.get("tenant_id", "default")).strip() or "default"
        message_id = str(payload.get("message_id", "")).strip()
        if not sender or "@" not in sender or not text or not message_id:
            raise ChannelError("Email relay payload is incomplete.")
        subject = str(payload.get("subject", "")).strip()
        combined = f"Subject: {subject}\n\n{text}" if subject else text
        return [InboundMessage(self.name, sender.casefold(), f"{tenant_id}|{sender.casefold()}", combined, PUBLIC,
                               raw=payload, tenant_id=tenant_id)]

    def send(self, thread_key: str, text: str) -> None:
        tenant_id = "default"
        if "|" in thread_key:
            tenant_id, thread_key = thread_key.split("|", 1)
        try:
            integrations.send_email(tenant_id, thread_key, "Re: your support request", text)
        except integrations.IntegrationError as exc:
            raise ChannelError(str(exc)) from None
