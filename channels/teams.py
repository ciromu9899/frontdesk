"""Microsoft Teams adapter, for the internal helpdesk persona.

## Why an outgoing webhook and not the Bot Framework

The Bot Framework is the route Microsoft documents first, and it authenticates
inbound requests with an RS256 JWT validated against a rotating JWKS. Doing that
correctly, in a project with no dependencies, would mean writing signature
verification from scratch - the one thing this project will not do. It also
requires an Azure bot registration and an app package before anything can be
tried at all.

A Teams **outgoing webhook** is added to a team by anyone who can manage it, no
Azure and no app review, and it authenticates with HMAC-SHA256 over the request
body using a secret Teams issues. That is the same shape as Slack's scheme and
Meta's, so it verifies with the same primitive and the same constant-time
comparison.

## The one structural difference

Slack and Meta take a message, return 200, and expect the reply to arrive later
over their API. A Teams outgoing webhook is **synchronous**: the reply is the body
of the HTTP response, and there is no outbound API to call - which also means
there is no bot token to store or leak. `send()` therefore raises, and
webhooks.py handles this channel inline.

## Trust

An outgoing webhook is scoped to one team inside one Microsoft 365 tenant, so a
message establishes "somebody inside this organisation" - the same claim Slack
supports, and the same `workspace` tier. It does not establish *which* employee,
so payments are outside the tier ceiling (see base.py).

Environment:
    FRONTDESK_TEAMS_SECURITY_TOKEN   the HMAC secret Teams shows when the
                                     outgoing webhook is created (base64)
    FRONTDESK_TEAMS_TENANT_ID        our tenant id (optional, recommended)
"""

from __future__ import annotations

import json
import os
import re

from channels.base import WORKSPACE, ChannelError, InboundMessage
from channels.signatures import verify_teams

# Teams wraps an @-mention of the bot in markup, and the model should not have to
# read around it: "<at>Helpdesk</at> where is my laptop" is just the question.
MENTION_RE = re.compile(r"<at>.*?</at>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")

# Teams renders the response body; anything longer is truncated by the client
# anyway, and a wall of text in a channel is its own problem.
MAX_REPLY_CHARS = 4000


class TeamsChannel:
    name = "teams"

    def __init__(self) -> None:
        self.security_token = os.environ.get("FRONTDESK_TEAMS_SECURITY_TOKEN", "")
        self.tenant_id = os.environ.get("FRONTDESK_TEAMS_TENANT_ID", "")

    def configured(self) -> bool:
        return bool(self.security_token)

    def verify(self, headers: dict, body: bytes) -> bool:
        return verify_teams(headers, body, self.security_token)

    def parse(self, body: bytes) -> list[InboundMessage]:
        """Normalise a Bot Framework activity.

        Dropped: anything that is not a message, our own output, activities from
        another tenant, and messages left empty once the mention markup is gone.
        """
        try:
            activity = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ChannelError("Teams payload was not valid JSON.") from None
        if not isinstance(activity, dict):
            raise ChannelError("Teams payload was not an activity.")

        if activity.get("type") != "message":
            return []

        channel_data = activity.get("channelData") or {}
        tenant = str((channel_data.get("tenant") or {}).get("id", ""))
        if self.tenant_id and tenant != self.tenant_id:
            return []          # another Microsoft 365 tenant

        sender = activity.get("from") or {}
        user = str(sender.get("id", ""))
        if not user:
            return []
        if sender.get("role") == "bot":
            return []          # our own output, echoed back

        text = MENTION_RE.sub(" ", str(activity.get("text", "")))
        text = TAG_RE.sub(" ", text)
        text = " ".join(text.split())
        if not text:
            return []          # an @-mention and nothing else

        conversation = str((activity.get("conversation") or {}).get("id", "")) or user
        return [InboundMessage(
            channel=self.name,
            external_user_id=user,
            thread_key=conversation,
            text=text,
            trust=WORKSPACE,
            display_name=str(sender.get("name", "")),
            raw=activity,
            tenant_id=f"teams:{tenant or self.tenant_id or 'default'}",
        )]

    def reply_payload(self, text: str) -> dict:
        """The activity Teams expects back as the HTTP response body."""
        return {"type": "message", "text": text[:MAX_REPLY_CHARS]}

    def send(self, thread_key: str, text: str) -> None:
        """Not available: a Teams outgoing webhook has no outbound API.

        This is deliberately an error rather than a silent no-op. A caller that
        reaches here believes it has replied to somebody and has not.
        """
        raise ChannelError(
            "Teams outgoing webhooks reply in the HTTP response, not through an "
            "API. Use reply_payload() and return it from the request handler.")
