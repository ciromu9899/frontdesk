"""Tenant-scoped salon appointment email reminders.

Run this from Task Scheduler or cron. A failed delivery remains pending so the
next run can retry it; a successful delivery is marked sent before another run.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import integrations
import tools


class ReminderError(RuntimeError):
    pass


def due(tenant_id: str, *, now: datetime | None = None,
        within_hours: int = 24) -> list[dict]:
    if within_hours < 1 or within_hours > 168:
        raise ReminderError("within_hours must be between 1 and 168.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ReminderError("now must include a timezone.")
    deadline = current.astimezone(timezone.utc) + timedelta(hours=within_hours)
    found = []
    store = tools.load_store(tenant_id)
    for reservation_id, reservation in store.get("reservations", {}).items():
        if (reservation.get("status") != "confirmed" or
                reservation.get("reminder_status") != "pending" or
                not reservation.get("reminders") or
                str(reservation.get("email", "")).count("@") != 1):
            continue
        try:
            local_zone = ZoneInfo(str(reservation.get("timezone", "")))
            local_start = datetime.strptime(
                f"{reservation['date']} {reservation['time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=local_zone)
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            continue
        start_utc = local_start.astimezone(timezone.utc)
        if current.astimezone(timezone.utc) < start_utc <= deadline:
            found.append({"reservation_id": reservation_id,
                          "starts_at": local_start.isoformat(), **reservation})
    return sorted(found, key=lambda item: item["starts_at"])


def send_due(tenant_id: str, *, now: datetime | None = None,
             within_hours: int = 24, dry_run: bool = False) -> dict:
    store = tools.load_store(tenant_id)
    candidates = due(tenant_id, now=now, within_hours=within_hours)
    sent = 0
    failed = 0
    for item in candidates:
        if dry_run:
            continue
        subject = f"Appointment reminder: {item.get('service', 'salon visit')}"
        message = (
            f"Hello {item.get('customer', '')},\n\n"
            f"This is a reminder for your appointment on {item['date']} at {item['time']} "
            f"({item.get('timezone', 'local time')}) with {item.get('staff', 'your stylist')} "
            f"at {item.get('location', 'the salon')}.\n\n"
            "Reply to the salon if you need help changing or cancelling it."
        )
        try:
            integrations.send_email(tenant_id, item["email"], subject, message)
        except integrations.IntegrationError:
            failed += 1
            continue
        reservation = store.get("reservations", {}).get(item["reservation_id"])
        if reservation and reservation.get("reminder_status") == "pending":
            reservation["reminder_status"] = "sent"
            reservation["reminder_sent_at"] = datetime.now(timezone.utc).isoformat()
            sent += 1
    if sent:
        tools.save_store(store, tenant_id)
    return {"due": len(candidates), "sent": sent, "failed": failed,
            "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send due salon appointment reminders")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--within-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = send_due(args.tenant, within_hours=args.within_hours,
                          dry_run=args.dry_run)
    except ReminderError as exc:
        print(str(exc))
        return 2
    print(json.dumps(result))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
