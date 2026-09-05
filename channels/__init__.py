"""Social channel integration.

base.py opens with the reasoning behind tying roles to trust tiers. The short
version: a sender arriving over public social media receives knowledge:read and
nothing else, so an order lookup or a payment is refused at the permission layer.
"""

from __future__ import annotations

from channels import identity, linkedin
from channels.base import (
    AUTHENTICATED,
    PUBLIC,
    WORKSPACE,
    Channel,
    ChannelError,
    InboundMessage,
    handoff_notice,
    roles_for,
)
from channels.dispatch import Dispatcher, principal_for, reset_sessions, sign_in_offer

__all__ = [
    "AUTHENTICATED", "PUBLIC", "WORKSPACE",
    "Channel", "ChannelError", "InboundMessage",
    "Dispatcher", "handoff_notice", "roles_for", "reset_sessions",
    "available", "identity", "linkedin", "principal_for", "sign_in_offer",
]


def available() -> dict:
    """Every known channel, configured or not. Used by --doctor and the receiver."""
    from channels.meta import MetaChannel
    from channels.slack import SlackChannel
    from channels.teams import TeamsChannel
    from channels.whatsapp import WhatsAppChannel
    from channels.email import EmailChannel
    from channels.github import GitHubChannel

    found = {}
    for factory in (GitHubChannel, SlackChannel, TeamsChannel, MetaChannel,
                    WhatsAppChannel, EmailChannel):
        channel = factory()
        found[channel.name] = channel
    return found
