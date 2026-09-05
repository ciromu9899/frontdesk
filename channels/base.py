"""Channel abstraction: bridges an inbound social message into a Frontdesk session.

## The decision this file exists to make

The CLI's confirmation gate is a person at a terminal. There is no terminal behind
an Instagram DM. Connect a bot to social media without answering "who approves the
refund when it is requested by DM", and money moves on the say-so of whoever is
holding the account.

So no new mechanism was added. A channel declares only what it was able to verify
about the sender, and receives a Principal that matches:

    public         anyone can write; a handle is a claim, not an identity
                   -> guest (public information only)
    workspace      membership of the organisation is verified
                   -> the configured role
    authenticated  the customer themselves is verified
                   -> the configured role

`guest` holds nothing but knowledge:read, so an order lookup, a reservation change
and a payment are all **refused at the permission layer**. Each tool's
`required_permission` does the work; there is no separate channel policy to keep in
sync. No configuration grants a public channel write access.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import auth

# Trust tiers.
PUBLIC = "public"
WORKSPACE = "workspace"
AUTHENTICATED = "authenticated"

# The most a tier may ever be granted. Configuration can lower this, never raise it.
#
# Note what `authenticated` does not include. Proving you are the customer is not
# authorisation to move money: a refund is the business acting, and it stays with
# an operator holding a token this system issued. A customer who verifies over a
# channel reaches their own records and stops there.
_CEILING = {
    PUBLIC: ("guest",),
    WORKSPACE: ("support", "operator"),
    AUTHENTICATED: ("support", "operator"),
}


class ChannelError(RuntimeError):
    """An inbound message failed verification, or an outbound send failed."""


@dataclass(frozen=True)
class InboundMessage:
    """One message from a channel, with the platform's shape already absorbed."""

    channel: str
    external_user_id: str        # identifier on the platform
    thread_key: str              # where a reply goes; the unit of conversation
    text: str
    trust: str = PUBLIC
    display_name: str = ""
    raw: dict = field(default_factory=dict)
    tenant_id: str = "default"

    def principal(self, tenant_id: str = "") -> auth.Principal:
        """Build the Principal this sender gets.

        The subject is always qualified by channel. A social handle is something
        anyone can claim, so it must never collide with an internal identity.
        """
        return auth.Principal(
            subject=f"{self.channel}:{self.external_user_id}",
            roles=roles_for(self.channel, self.trust),
            tenant_id=tenant_id or self.tenant_id or "default",
        )


def roles_for(channel: str, trust: str) -> tuple[str, ...]:
    """Resolve the roles actually granted, from the trust tier and configuration.

    FRONTDESK_CHANNEL_<CHANNEL>_ROLES can narrow the grant, but anything above the
    tier's ceiling is dropped without comment - a misconfiguration must not be able
    to hand a public channel the ability to move money.
    """
    ceiling = _CEILING.get(trust, (
        "guest",))
    configured = os.environ.get(f"FRONTDESK_CHANNEL_{channel.upper()}_ROLES", "")
    if not configured:
        return (ceiling[0],)
    requested = tuple(part.strip() for part in configured.split(",") if part.strip())
    allowed = tuple(role for role in requested
                    if role in ceiling and role in auth.ROLE_PERMISSIONS)
    return allowed or (ceiling[0],)


class Channel(Protocol):
    """The contract every platform adapter satisfies."""

    name: str

    def verify(self, headers: dict, body: bytes) -> bool:
        """Check the signature. The first gate against a forged message."""
        ...

    def parse(self, body: bytes) -> list[InboundMessage]:
        """Normalise a platform payload into InboundMessage values."""
        ...

    def send(self, thread_key: str, text: str) -> None:
        """Send a reply."""
        ...


def handoff_notice(channel: str) -> str:
    """What a public channel says when the permission layer refuses the work.

    Refusing customer-specific work on public social media is the design, not a
    fault - so this never stops at "I can't". It hands the person a route to
    someone who can.
    """
    return (
        "I can answer general questions here, but I can't look up or change an "
        "order over social media - I have no way to confirm who you are. "
        "Message us from your account page, or reply here and a teammate will "
        "pick this up."
    )
