"""Approvals that can be answered from somewhere other than the terminal.

## What this is for

The confirmation gate is the product: nothing irreversible runs without a person
saying yes. Until now that person had to be at a terminal, which meant the gate
worked perfectly for the CLI and refused everything on a channel - a refund asked
for in an Instagram DM at 9pm was declined not because it was wrong but because
nobody was sitting at a keyboard.

That is the right failure, and it is still a failure. The owner of a small
business is not at a terminal; they are holding a phone. So an action can now be
parked here, shown on that phone, and answered with one tap while the agent waits.

## The rules that make a tap worth as much as a keypress

- **The approver must be allowed to do the thing themselves.** A phone approval
  carries the permission of whoever taps it; someone without `payments:write`
  cannot authorise a refund, which is a stronger position than the terminal,
  where whoever sits down can approve anything.
- **One decision each.** A request that has been answered is closed. Tapping twice
  does not run it twice.
- **Silence is a no.** An unanswered request expires and the action does not run.
  Waiting forever would hold the conversation open indefinitely, and defaulting
  to yes would make the whole gate decorative.
- **Everything is audited** - requested, decided, expired, and by whom.

## Where these live

In memory, in the process running the agent. A parked approval has a thread
waiting on it, and a thread cannot be resumed from another process. So the
approval screen is served by that same process rather than being a separate
service that would have nothing to resume.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import audit
import auth

# How long an unanswered request stays open. Long enough to reach for a phone,
# short enough that a customer is not left waiting on a dead conversation.
DEFAULT_TIMEOUT_SECONDS = 5 * 60

PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"
EXPIRED = "expired"

# Keeps the list on screen bounded, and stops a flood from exhausting memory.
MAX_OPEN = 200


@dataclass
class Approval:
    """One irreversible action, waiting for a person."""

    id: str
    summary: str                 # the same line the terminal would print
    tool: str
    permission: str              # what the approver must themselves hold
    requested_by: str            # the principal the agent was acting as
    channel: str
    session_id: str
    created_at: float
    expires_at: float
    state: str = PENDING
    decided_by: str = ""
    decided_at: float = 0.0
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_open(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.state == PENDING and current < self.expires_at

    def as_dict(self, now: float | None = None) -> dict:
        current = time.time() if now is None else now
        return {
            "id": self.id,
            "summary": self.summary,
            "tool": self.tool,
            "permission": self.permission,
            "requested_by": self.requested_by,
            "channel": self.channel,
            "state": self.state,
            "waiting_seconds": int(current - self.created_at),
            "expires_in": max(0, int(self.expires_at - current)),
            "decided_by": self.decided_by,
        }


_OPEN: dict[str, Approval] = {}
_LOCK = threading.Lock()


def request(summary: str, *, tool: str, permission: str, requested_by: str,
            channel: str = "", session_id: str = "",
            timeout: float = DEFAULT_TIMEOUT_SECONDS,
            now: float | None = None) -> Approval:
    """Park an action and return the record the approver will see."""
    current = time.time() if now is None else now
    approval = Approval(
        id=uuid.uuid4().hex[:12],
        summary=summary,
        tool=tool,
        permission=permission,
        requested_by=requested_by,
        channel=channel,
        session_id=session_id,
        created_at=current,
        expires_at=current + timeout,
    )
    with _LOCK:
        _expire_locked(current)
        if len(_OPEN) >= MAX_OPEN:
            oldest = min(_OPEN.values(), key=lambda a: a.created_at)
            oldest.state = EXPIRED
            oldest._event.set()
            _OPEN.pop(oldest.id, None)
        _OPEN[approval.id] = approval
    audit.record("approval.requested", actor=requested_by, session_id=session_id,
                 details={"id": approval.id, "tool": tool, "summary": summary,
                          "channel": channel})
    return approval


def wait(approval: Approval) -> str:
    """Block until somebody decides, or the request expires. Returns the state."""
    remaining = approval.expires_at - time.time()
    if remaining > 0:
        approval._event.wait(remaining)

    with _LOCK:
        if approval.state == PENDING:
            approval.state = EXPIRED
        _OPEN.pop(approval.id, None)
        state = approval.state

    if state == EXPIRED:
        audit.record("approval.expired", actor=approval.requested_by,
                     session_id=approval.session_id,
                     details={"id": approval.id, "tool": approval.tool,
                              "summary": approval.summary})
    return state


def decide(approval_id: str, approved: bool, approver: auth.Principal,
           now: float | None = None) -> tuple[bool, str]:
    """Answer a request. Returns (whether it took, why not).

    The approver has to hold the permission the tool needs. Tapping a button is
    not a way around the role that would have been required to run the action
    directly.
    """
    current = time.time() if now is None else now
    with _LOCK:
        _expire_locked(current)
        approval = _OPEN.get(approval_id)
        if approval is None:
            return False, "That request is no longer open."
        if approval.state != PENDING:
            return False, "That request has already been answered."
        if current >= approval.expires_at:
            approval.state = EXPIRED
            approval._event.set()
            _OPEN.pop(approval_id, None)
            return False, "That request expired before it was answered."
        if approval.permission and not approver.can(approval.permission):
            # Deliberately said out loud rather than hidden: the approver needs to
            # know to fetch somebody who can, not that the button is broken.
            audit.record("approval.refused", actor=approver.subject,
                         details={"id": approval_id, "tool": approval.tool,
                                  "needed": approval.permission})
            return False, (f"You do not have {approval.permission}, so you cannot "
                           "authorise this one.")

        approval.state = APPROVED if approved else DECLINED
        approval.decided_by = approver.subject
        approval.decided_at = current
        approval._event.set()
        _OPEN.pop(approval_id, None)

    audit.record("approval.decided", actor=approver.subject,
                 session_id=approval.session_id,
                 details={"id": approval_id, "tool": approval.tool,
                          "summary": approval.summary,
                          "decision": approval.state,
                          "requested_by": approval.requested_by})
    return True, ""


def pending(now: float | None = None) -> list[dict]:
    """Everything still waiting, oldest first."""
    current = time.time() if now is None else now
    with _LOCK:
        _expire_locked(current)
        open_now = sorted(_OPEN.values(), key=lambda a: a.created_at)
        return [approval.as_dict(current) for approval in open_now]


def _expire_locked(now: float) -> None:
    """Drop anything past its deadline. The caller holds the lock."""
    for approval in list(_OPEN.values()):
        if now >= approval.expires_at:
            if approval.state == PENDING:
                approval.state = EXPIRED
            approval._event.set()
            _OPEN.pop(approval.id, None)


def reset() -> None:
    """For tests and restarts. Anything waiting is released as expired."""
    with _LOCK:
        for approval in list(_OPEN.values()):
            if approval.state == PENDING:
                approval.state = EXPIRED
            approval._event.set()
        _OPEN.clear()


def enabled() -> bool:
    """Whether remote approval is switched on for this process."""
    import os
    return os.environ.get("FRONTDESK_REMOTE_APPROVAL") == "1"
