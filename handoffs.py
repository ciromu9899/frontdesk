"""Persistent human-handoff queue.

The queue is an append-only JSONL event stream. Opening or resolving a handoff
adds an event; existing records are never rewritten, so restarts do not lose
work and an operator can reconstruct what happened.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import audit
import state
from config import DATA_DIR


ROOT = Path(__file__).resolve().parent
HANDOFF_PATH = DATA_DIR / "handoffs.jsonl"
MAX_SUMMARY_CHARS = 2_000
MAX_NOTE_CHARS = 1_000
_LOCK = threading.Lock()


class HandoffError(Exception):
    """A handoff request or decision could not be accepted."""


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _append(event: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _conversation_key(ticket: dict) -> str:
    channel = str(ticket.get("channel") or "web")
    thread_key = str(ticket.get("thread_key") or ticket.get("id") or "")
    if channel != "web" and not thread_key.startswith(channel + ":"):
        return f"{channel}:{thread_key}"
    return thread_key


def request(
    summary: str,
    *,
    requested_by: str,
    tenant_id: str = "default",
    channel: str = "",
    thread_key: str = "",
    session_id: str = "",
    reason: str = "unresolved",
    path: Path | None = None,
) -> dict:
    """Open and persist one human-handoff ticket."""
    summary = _clean(summary, MAX_SUMMARY_CHARS)
    if not summary:
        raise HandoffError("A handoff summary is required.")
    ticket_id = f"H-{uuid.uuid4().hex[:12].upper()}"
    event = {
        "event": "opened",
        "id": ticket_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "summary": summary,
        "reason": _clean(reason, 80) or "unresolved",
        "requested_by": _clean(requested_by, 320),
        "tenant_id": _clean(tenant_id, 160) or "default",
        "channel": _clean(channel, 80),
        "thread_key": _clean(thread_key, 320),
        "session_id": _clean(session_id, 160),
    }
    destination = path or HANDOFF_PATH
    with _LOCK:
        _append(event, destination)
    if path is None:
        conversation_key = _conversation_key(event)
        state.upsert_thread(event["tenant_id"], conversation_key, channel=event["channel"] or "web",
                            customer_id=event["requested_by"], subject=summary,
                            status_value="open", metadata={"handoff_id": ticket_id})
    audit.record(
        "handoff.opened",
        actor=event["requested_by"] or "system",
        tenant_id=event["tenant_id"],
        session_id=event["session_id"],
        details={"handoff_id": ticket_id, "reason": event["reason"],
                 "channel": event["channel"]},
    )
    return event


def _events(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("id"):
                yield event


def list_tickets(
    *,
    status: str | None = "open",
    tenant_id: str | None = None,
    limit: int = 100,
    path: Path | None = None,
) -> list[dict]:
    """Return the latest state of matching tickets, newest first."""
    destination = path or HANDOFF_PATH
    with _LOCK:
        current: dict[str, dict] = {}
        for event in _events(destination):
            ticket_id = str(event["id"])
            if event.get("event") == "opened":
                current[ticket_id] = dict(event)
            elif ticket_id in current:
                kind = event.get("event")
                if kind == "resolved":
                    current[ticket_id].update({"status": "resolved",
                        "resolved_at": event.get("timestamp", ""),
                        "resolved_by": event.get("resolved_by", ""),
                        "resolution_note": event.get("note", "")})
                elif kind == "reopened":
                    current[ticket_id].update({"status": "open", "reopened_at": event.get("timestamp", "")})
                elif kind == "started":
                    current[ticket_id].update({"status": "in_progress", "started_at": event.get("timestamp", ""),
                                               "assignee": event.get("actor", "")})
                elif kind == "assigned":
                    current[ticket_id]["assignee"] = event.get("assignee", "")
                elif kind == "note":
                    current[ticket_id].setdefault("notes", []).append({
                        "timestamp": event.get("timestamp", ""), "actor": event.get("actor", ""),
                        "note": event.get("note", "")})
    tickets = [ticket for ticket in current.values()
               if (status is None or ticket.get("status") == status)
               and (tenant_id is None or ticket.get("tenant_id") == tenant_id)]
    tickets.sort(key=lambda ticket: str(ticket.get("timestamp", "")), reverse=True)
    return tickets[:max(0, min(limit, 1_000))]


def resolve(
    ticket_id: str,
    *,
    resolved_by: str,
    tenant_id: str = "default",
    note: str = "",
    path: Path | None = None,
) -> bool:
    """Resolve an open ticket once. Returns False if it is absent or already closed."""
    destination = path or HANDOFF_PATH
    ticket_id = _clean(ticket_id, 80)
    with _LOCK:
        current: dict | None = None
        for event in _events(destination):
            if event.get("id") != ticket_id:
                continue
            if event.get("event") == "opened":
                current = event
            elif event.get("event") == "resolved":
                current = None
        if current is None or current.get("tenant_id") != tenant_id:
            return False
        event = {
            "event": "resolved",
            "id": ticket_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resolved_by": _clean(resolved_by, 320),
            "tenant_id": tenant_id,
            "note": _clean(note, MAX_NOTE_CHARS),
        }
        _append(event, destination)
    if path is None:
        conversation_key = _conversation_key(current)
        state.upsert_thread(tenant_id, conversation_key, status_value="resolved",
                            metadata={"handoff_id": ticket_id})
    audit.record(
        "handoff.resolved",
        actor=event["resolved_by"] or "system",
        tenant_id=tenant_id,
        session_id=str(current.get("session_id", "")),
        details={"handoff_id": ticket_id},
    )
    return True


def update(ticket_id: str, action: str, *, actor: str, tenant_id: str = "default",
           assignee: str = "", note: str = "", path: Path | None = None) -> bool:
    """Assign, start, annotate, or reopen a ticket without rewriting history."""
    if action not in {"assigned", "started", "note", "reopened"}:
        raise HandoffError("Unsupported handoff action.")
    destination = path or HANDOFF_PATH
    ticket_id = _clean(ticket_id, 80)
    tickets = list_tickets(status=None, tenant_id=tenant_id, limit=1_000, path=destination)
    current = next((ticket for ticket in tickets if ticket.get("id") == ticket_id), None)
    if current is None or (current.get("status") == "resolved" and action != "reopened"):
        return False
    event = {"event": action, "id": ticket_id,
             "timestamp": datetime.now(timezone.utc).isoformat(),
             "tenant_id": tenant_id, "actor": _clean(actor, 320)}
    if action == "assigned":
        event["assignee"] = _clean(assignee, 320)
        if not event["assignee"]:
            raise HandoffError("An assignee is required.")
    if action == "note":
        event["note"] = _clean(note, MAX_NOTE_CHARS)
        if not event["note"]:
            raise HandoffError("A note is required.")
    with _LOCK:
        _append(event, destination)
    if path is None:
        conversation_key = _conversation_key(current)
        status_value = {"started": "in_progress", "reopened": "open"}.get(action)
        assigned_to = event.get("assignee") if action == "assigned" else (
            event.get("actor") if action == "started" else None)
        state.upsert_thread(tenant_id, conversation_key, status_value=status_value,
                            assignee=assigned_to, metadata={"handoff_id": ticket_id})
        if action == "note":
            state.append_message(tenant_id, conversation_key, "note", event["note"],
                                 sender_id=event["actor"], channel=str(current.get("channel") or "web"))
    audit.record(f"handoff.{action}", actor=event["actor"] or "system", tenant_id=tenant_id,
                 details={"handoff_id": ticket_id, "assignee": event.get("assignee", "")})
    return True
