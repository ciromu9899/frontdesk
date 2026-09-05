"""Route an inbound message through a Frontdesk session and return the reply.

Sessions are kept per conversation. Social enquiries go back and forth, and a
fresh session on every message forgets what was just said, which makes the bot
useless in exactly the situation it exists for.

A turn that ends short on permissions is treated as a handoff, not a failure.
Refusing customer-specific work on a public channel is the design, so the reply
carries a route to a human rather than stopping at "no".
"""

from __future__ import annotations

import io
import os
import threading
import time

import audit
import auth
import chat
import config as cfg
import handoffs
import state
from channels import identity, linkedin
from channels.base import PUBLIC, InboundMessage, handoff_notice, roles_for

# Default persona per channel: an internal Slack and a public DM owe their
# senders different boundaries.
DEFAULT_PERSONA = {
    "github": "github-support",
    "slack": "helpdesk",
    "teams": "helpdesk",
    "meta": "ecommerce",
    "whatsapp": "ecommerce",
    "email": "helpdesk",
}

# One session per conversation.
_SESSIONS: dict[str, chat.Session] = {}
_LOCK = threading.Lock()

# Cap on retained sessions; unbounded growth would eat memory.
MAX_SESSIONS = 500


def principal_for(message: InboundMessage) -> auth.Principal:
    """The Principal this message runs as, after any completed sign-in.

    A verification that expired, or was never made, leaves the message exactly
    where the channel put it. The stored tier is re-run through roles_for, so the
    ceiling in base.py still applies to a signed-in person - remembering a
    verification must not become a way around it.
    """
    record = identity.recall(message.channel, message.external_user_id,
                             tenant_id=message.tenant_id)
    if record is None:
        return message.principal()
    return auth.Principal(
        subject=str(record.get("subject", "")) or message.principal().subject,
        roles=roles_for(message.channel, str(record.get("trust", PUBLIC))),
        tenant_id=message.tenant_id,
    )


class Dispatcher:
    def __init__(self, persona: str | None = None, provider: str = "auto") -> None:
        self.persona_override = persona
        self.provider = provider

    def _session(self, message: InboundMessage) -> chat.Session:
        conversation_key = f"{message.channel}:{message.thread_key}"
        key = f"{message.tenant_id}:{conversation_key}"
        with _LOCK:
            existing = _SESSIONS.get(key)
            if existing is not None:
                return existing

            if len(_SESSIONS) >= MAX_SESSIONS:
                _SESSIONS.pop(next(iter(_SESSIONS)))   # evict the oldest

            persona = self.persona_override or DEFAULT_PERSONA.get(
                message.channel, "default")
            config = cfg.Config(provider=self.provider, persona=persona, use_tools=True,
                                max_tokens=max(64, min(int(os.environ.get(
                                    "FRONTDESK_CHANNEL_MAX_TOKENS", "256")), 4096)),
                                max_history_chars=60_000).resolve()
            # A social channel is not a terminal, so no confirmation can be shown.
            # Actions needing approval are therefore not executed - Session._approve
            # declines when it cannot ask.
            #
            # Its running commentary is discarded rather than printed. On a server
            # that stdout is a second copy of every customer message, unredacted,
            # unrotated and interleaved across threads; audit.py is the record
            # that is meant to exist.
            session = chat.Session(
                config, chat.Style(enabled=False), principal_for(message),
                out=io.StringIO(),
                context={"channel": message.channel, "thread_key": message.thread_key,
                         "tenant_id": message.tenant_id},
            )
            saved = state.load_session(message.tenant_id, conversation_key)
            if saved:
                session.session_id = str(saved["session_id"])
                session.history = chat.Session.deserialize_history(saved["history"])
            _SESSIONS[key] = session
            return session

    def handle(self, message: InboundMessage) -> str:
        """Handle one message and return the text to reply with."""
        session = self._session(message)

        # Somebody may have signed in - or had their verification expire - since
        # this session was created. Permissions are read from the principal at
        # every tool call, so replacing it takes effect on this message.
        current = principal_for(message)
        if (current.subject, current.roles) != (session.principal.subject,
                                                session.principal.roles):
            session.principal = current

        audit.record(
            "channel.received", actor=session.principal.subject,
            tenant_id=session.principal.tenant_id, session_id=session.session_id,
            details={"channel": message.channel, "trust": message.trust,
                     "chars": len(message.text)},
        )

        conversation_key = f"{message.channel}:{message.thread_key}"
        state.append_message(session.principal.tenant_id, conversation_key, "customer",
                             message.text, sender_id=session.principal.subject,
                             channel=message.channel,
                             metadata={"source": message.raw.get("_frontdesk_source", ""),
                                       "event": message.raw.get("_frontdesk_event", "")})
        current_thread = state.get_thread(session.principal.tenant_id, conversation_key)
        if current_thread and current_thread.get("status") == "in_progress":
            state.record_metric(session.principal.tenant_id, "message_during_human_takeover",
                                conversation_key=conversation_key,
                                dimensions={"channel": message.channel})
            return ""

        started = time.monotonic()
        reply = session.ask(message.text)
        elapsed = time.monotonic() - started
        state.save_session(
            session.principal.tenant_id,
            f"{message.channel}:{message.thread_key}",
            session.durable_payload(),
        )

        if not reply:
            # An empty reply means either the permission layer refused the work or
            # the provider failed. On a public channel the former is routine, so
            # hand the person a route forward rather than stopping at "no".
            if session.principal.roles == ("guest",):
                offer = sign_in_offer(message)
                if offer:
                    return offer
            ticket_id = self._handoff(message, session)
            reference = f" Reference: {ticket_id}." if ticket_id else ""
            if session.principal.roles == ("guest",):
                return handoff_notice(message.channel) + reference
            return "Something went wrong on our side. A teammate will follow up." + reference
        state.append_message(session.principal.tenant_id, conversation_key, "assistant",
                             reply, sender_id="frontdesk", channel=message.channel)
        state.record_metric(session.principal.tenant_id, "assistant_reply",
                            conversation_key=conversation_key,
                            value=elapsed,
                            dimensions={"channel": message.channel})
        return reply

    @staticmethod
    def _handoff(message: InboundMessage, session: chat.Session) -> str:
        """Persist a safe fallback without copying the customer's message."""
        try:
            ticket = handoffs.request(
                "The automated conversation could not produce a reply; review the channel thread.",
                requested_by=session.principal.subject,
                tenant_id=session.principal.tenant_id,
                channel=message.channel,
                thread_key=message.thread_key,
                session_id=session.session_id,
                reason="system_error",
            )
            return str(ticket["id"])
        except (handoffs.HandoffError, OSError):
            return ""


def sign_in_offer(message: InboundMessage) -> str:
    """Offer to establish identity, when there is a way to establish it.

    This is the reason the LinkedIn integration exists. Without it the refusal on
    a public channel is a dead end; with it, the person has something to do about
    it in the same breath.
    """
    if not linkedin.configured():
        return ""
    try:
        url = linkedin.authorization_url(
            message.channel, message.external_user_id, message.thread_key,
            tenant_id=message.tenant_id)
    except linkedin.LinkedInError:
        return ""
    return (
        "I can't look up or change an order over social media - I have no way to "
        "confirm who you are from a handle alone. You can verify yourself in a "
        "few seconds by signing in with LinkedIn, and then I can help right here:"
        f"\n\n{url}\n\n"
        "The link is good for 15 minutes and only applies to this conversation. "
        "If you'd rather not, reply here and a teammate will pick this up."
    )


def reset_sessions() -> None:
    """For tests and restarts."""
    with _LOCK:
        _SESSIONS.clear()
