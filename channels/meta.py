"""Meta adapter (Instagram DM and Facebook Messenger), for DTC commerce.

A large share of a DTC brand's support arrives as Instagram DMs. But the sender of
a DM is not "the customer who placed that order" - it is "whoever is operating that
account". Accounts get shared, and accounts get taken over.

So this channel is pinned to the public tier. Order lookups, reservation changes
and payments are all refused at the permission layer, and the person is pointed at
a route where identity can actually be established: the account page, or a human.
That is not a limitation bolted on; it is an accurate statement of what this
channel can verify.

Environment:
    FRONTDESK_META_APP_SECRET      signature verification
    FRONTDESK_META_PAGE_TOKEN      sending replies
    FRONTDESK_META_VERIFY_TOKEN    the subscription challenge
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import resilience

from channels.base import PUBLIC, ChannelError, InboundMessage
from channels.signatures import verify_meta

DEFAULT_GRAPH_VERSION = "v26.0"


def graph_base() -> str:
    version = os.environ.get("FRONTDESK_META_GRAPH_VERSION", DEFAULT_GRAPH_VERSION)
    if not re.fullmatch(r"v\d{1,2}\.\d", version):
        raise ChannelError("FRONTDESK_META_GRAPH_VERSION is invalid.")
    return f"https://graph.facebook.com/{version}"


class MetaChannel:
    """Instagram and Messenger share a webhook shape, so one adapter serves both."""

    name = "meta"

    def __init__(self) -> None:
        self.app_secret = os.environ.get("FRONTDESK_META_APP_SECRET", "")
        self.page_token = os.environ.get("FRONTDESK_META_PAGE_TOKEN", "")
        self.verify_token = os.environ.get("FRONTDESK_META_VERIFY_TOKEN", "")
        self._circuit = resilience.CircuitBreaker()

    def configured(self) -> bool:
        return bool(self.app_secret and self.page_token)

    def verify(self, headers: dict, body: bytes) -> bool:
        return verify_meta(headers, body, self.app_secret)

    def parse(self, body: bytes) -> list[InboundMessage]:
        """Normalise messaging events.

        Dropped: echoes of our own sends, read and delivery receipts, and
        attachment-only messages with no text.
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ChannelError("Meta payload was not valid JSON.") from None

        messages: list[InboundMessage] = []
        for entry in payload.get("entry", []):
            tenant_id = f"meta:{entry.get('id') or 'default'}"
            for event in entry.get("messaging", []):
                message = event.get("message") or {}
                if message.get("is_echo"):
                    continue                     # our own send
                if not message.get("text"):
                    continue                     # attachment only, or a receipt
                sender = str((event.get("sender") or {}).get("id", ""))
                if not sender:
                    continue
                messages.append(InboundMessage(
                    channel=self.name,
                    external_user_id=sender,
                    thread_key=sender,           # Messenger replies go to the sender id
                    text=str(message["text"]).strip(),
                    trust=PUBLIC,                # a DM sender may not be the customer
                    raw=event,
                    tenant_id=tenant_id,
                ))
        return messages

    def challenge(self, query: dict) -> str | None:
        """Answer the GET challenge sent when the subscription is registered."""
        from channels.signatures import meta_verify_challenge
        return meta_verify_challenge(query, self.verify_token)

    def send(self, thread_key: str, text: str) -> None:
        if not self.page_token:
            raise ChannelError("FRONTDESK_META_PAGE_TOKEN is not set.")
        payload = {
            "recipient": {"id": thread_key},
            "message": {"text": text[:1000]},
            "messaging_type": "RESPONSE",   # declare this as a reply to an enquiry
        }
        url = f"{graph_base()}/me/messages?" + urllib.parse.urlencode(
            {"access_token": self.page_token})
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        def perform() -> None:
            # graph_base is pinned to Meta's HTTPS origin and validates the version.
            with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
                response.read()

        try:
            resilience.execute(perform, retry_safe=False, breaker=self._circuit)
        except resilience.CircuitOpenError:
            raise ChannelError("Meta circuit is open after repeated failures.") from None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ChannelError(f"Meta rejected the message ({exc.code}): {detail}") from None
        except urllib.error.URLError as exc:
            raise ChannelError(f"Could not reach Meta: {exc.reason}") from None
