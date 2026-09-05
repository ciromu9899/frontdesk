"""Slack adapter, for the internal helpdesk persona.

Slack sits at the workspace tier because two things are checkable: the signature,
and that team_id matches our own workspace. That establishes "somebody inside this
company" - a materially different claim from a handle on public social media.

It does not establish *which* employee, so payment permissions are excluded from
the tier ceiling (see _CEILING in base.py).

Environment:
    FRONTDESK_SLACK_SIGNING_SECRET   signature verification
    FRONTDESK_SLACK_BOT_TOKEN        sending replies (xoxb-...)
    FRONTDESK_SLACK_TEAM_ID          our workspace id (optional, recommended)
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

import resilience

try:
    import truststore
except ImportError:  # Optional outside the Windows desktop distribution.
    truststore = None

from channels.base import WORKSPACE, ChannelError, InboundMessage
from channels.signatures import verify_slack

API_BASE = "https://slack.com/api"


def _ssl_context() -> ssl.SSLContext:
    """Use the OS trust store when available without weakening TLS checks."""
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


class SlackChannel:
    name = "slack"

    def __init__(self) -> None:
        self.signing_secret = os.environ.get("FRONTDESK_SLACK_SIGNING_SECRET", "")
        self.bot_token = os.environ.get("FRONTDESK_SLACK_BOT_TOKEN", "")
        self.team_id = os.environ.get("FRONTDESK_SLACK_TEAM_ID", "")
        self._circuit = resilience.CircuitBreaker()

    def configured(self) -> bool:
        return bool(self.signing_secret and self.bot_token)

    def verify(self, headers: dict, body: bytes) -> bool:
        return verify_slack(headers, body, self.signing_secret)

    def parse(self, body: bytes) -> list[InboundMessage]:
        """Normalise an Events API payload.

        Dropped: the bot's own messages (which would loop forever), edit and
        delete notifications, and anything from another workspace.
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ChannelError("Slack payload was not valid JSON.") from None

        if self.team_id and payload.get("team_id") != self.team_id:
            return []          # a different workspace

        event = payload.get("event") or {}
        if event.get("type") not in ("message", "app_mention"):
            return []
        if event.get("bot_id") or event.get("subtype"):
            return []          # bot output, edits, deletions, join notices

        text = str(event.get("text", "")).strip()
        user = str(event.get("user", ""))
        channel_id = str(event.get("channel", ""))
        if not text or not user or not channel_id:
            return []

        # Reply into the same thread when the message was in one.
        thread = str(event.get("thread_ts") or event.get("ts") or "")
        return [InboundMessage(
            channel=self.name,
            external_user_id=user,
            thread_key=f"{channel_id}:{thread}",
            text=text,
            trust=WORKSPACE,
            raw=event,
            tenant_id=f"slack:{payload.get('team_id') or self.team_id or 'default'}",
        )]

    def send(self, thread_key: str, text: str) -> None:
        if not self.bot_token:
            raise ChannelError("FRONTDESK_SLACK_BOT_TOKEN is not set.")
        channel_id, _, thread_ts = thread_key.partition(":")
        payload = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        self._post("chat.postMessage", payload)

    def _post(self, method: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{API_BASE}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.bot_token}",
                     "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        def perform() -> dict:
            # API_BASE is a module constant pinned to Slack's HTTPS origin.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=15, context=_ssl_context()
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            result = resilience.execute(
                perform, retry_safe=False, breaker=self._circuit)
        except resilience.CircuitOpenError:
            raise ChannelError("Slack circuit is open after repeated failures.") from None
        except urllib.error.URLError as exc:
            raise ChannelError(f"Could not reach Slack: {exc}") from None
        if not result.get("ok"):
            raise ChannelError(f"Slack rejected the call: {result.get('error')}")
        return result
