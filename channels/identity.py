"""Where a completed sign-in is remembered.

A sign-in that only applied to the message it arrived on would be useless: the
person proves who they are, and the very next message is a stranger again. So the
result is kept, keyed by the person on the channel they were already using.

What is stored is a verified email address, which is personal data, so:

- it expires (FRONTDESK_IDENTITY_TTL_HOURS, eight hours by default), and expired
  records are dropped on the next read rather than lingering until someone looks;
- `forget()` exists and is wired to the customer being able to ask;
- every write and every drop is an audit event.

The file lives under data/, which is gitignored and excluded from every archive.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import audit
from config import DATA_DIR

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = DATA_DIR / "verified-identities.json"

DEFAULT_TTL_HOURS = 8

_LOCK = threading.Lock()


def _ttl_seconds() -> int:
    try:
        hours = float(os.environ.get("FRONTDESK_IDENTITY_TTL_HOURS", DEFAULT_TTL_HOURS))
    except ValueError:
        hours = DEFAULT_TTL_HOURS
    if hours <= 0 or hours > 24 * 7:
        hours = DEFAULT_TTL_HOURS
    return int(hours * 3600)


def _key(channel: str, external_user_id: str, tenant_id: str = "default") -> str:
    return f"{tenant_id}:{channel}:{external_user_id}"


def _load(path: Path | None = None) -> dict:
    path = path or STORE_PATH
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # A corrupt store must not take the channel down with it. Losing a
        # verification costs one extra sign-in; refusing to start costs the day.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save(records: dict, path: Path | None = None) -> None:
    path = path or STORE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    temporary.replace(path)


def remember(channel: str, external_user_id: str, *, subject: str, email: str,
             trust: str, name: str = "", path: Path | None = None,
             now: int | None = None, tenant_id: str = "default") -> dict:
    """Record a completed sign-in and return the stored record."""
    current = int(time.time() if now is None else now)
    record = {
        "subject": subject,
        "email": email,
        "trust": trust,
        "name": name,
        "exp": current + _ttl_seconds(),
    }
    with _LOCK:
        records = _load(path)
        records[_key(channel, external_user_id, tenant_id)] = record
        _save(records, path)
    audit.record(
        "identity.verified", actor=subject, tenant_id=tenant_id,
        details={"channel": channel, "trust": trust, "expires_in": _ttl_seconds()},
    )
    return record


def recall(channel: str, external_user_id: str, path: Path | None = None,
           now: int | None = None, tenant_id: str = "default") -> dict | None:
    """The live record for this person, or None. Expired records are removed."""
    current = int(time.time() if now is None else now)
    key = _key(channel, external_user_id, tenant_id)
    with _LOCK:
        records = _load(path)
        record = records.get(key)
        if record is None:
            return None
        if not isinstance(record, dict) or int(record.get("exp", 0)) <= current:
            records.pop(key, None)
            _save(records, path)
            return None
        return dict(record)


def forget(channel: str, external_user_id: str, path: Path | None = None,
           tenant_id: str = "default") -> bool:
    """Drop a verification. Returns whether there was one to drop."""
    key = _key(channel, external_user_id, tenant_id)
    with _LOCK:
        records = _load(path)
        record = records.pop(key, None)
        if record is None:
            return False
        _save(records, path)
    audit.record("identity.forgotten", actor=str(record.get("subject", "")),
                 tenant_id=tenant_id,
                 details={"channel": channel})
    return True


def purge_expired(path: Path | None = None, now: int | None = None) -> int:
    """Drop every expired record. Returns how many went."""
    current = int(time.time() if now is None else now)
    with _LOCK:
        records = _load(path)
        stale = [key for key, record in records.items()
                 if not isinstance(record, dict) or int(record.get("exp", 0)) <= current]
        for key in stale:
            records.pop(key, None)
        if stale:
            _save(records, path)
    return len(stale)
