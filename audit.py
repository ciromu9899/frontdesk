"""Append-only JSONL audit log with redaction and a SHA-256 hash chain.

Past a size limit the log rolls into a new segment. The first event written after
a roll carries the previous segment's final hash as its previous_hash, so the
chain runs unbroken across files and tampering stays detectable either side of a
rotation.

Reads walk only as far as they need; the log is never loaded whole.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR


ROOT = Path(__file__).resolve().parent
AUDIT_PATH = DATA_DIR / "audit.jsonl"
_LOCK = threading.Lock()
_SENSITIVE_KEY = re.compile(r"token|secret|password|authorization|api[_-]?key|card|cvv", re.I)
GENESIS = "0" * 64


def max_bytes() -> int:
    """Bytes per segment. Zero or less disables rotation."""
    try:
        return int(os.environ.get("FRONTDESK_AUDIT_MAX_BYTES", 5 * 1024 * 1024))
    except ValueError:
        return 5 * 1024 * 1024


def segments(path: Path) -> list[Path]:
    """Rotated segments, oldest first. The active file is not included."""
    return sorted(path.parent.glob(f"{path.stem}-*{path.suffix}"))


def _rotate(path: Path) -> None:
    """Move the active file aside. The next previous_hash carries the chain over."""
    limit = max_bytes()
    if limit <= 0 or not path.exists() or path.stat().st_size < limit:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path.rename(path.with_name(f"{path.stem}-{stamp}{path.suffix}"))



def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def _canonical(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _last_hash(path: Path) -> str:
    if not path.exists():
        return GENESIS
    reversed_line = bytearray()
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position:
            position -= 1
            handle.seek(position)
            value = handle.read(1)
            if value in (b"\n", b"\r"):
                if reversed_line:
                    break
                continue
            reversed_line.extend(value)
    if not reversed_line:
        return GENESIS
    try:
        last = bytes(reversed(reversed_line)).decode("utf-8")
        candidate = str(json.loads(last).get("hash", GENESIS))
        if len(candidate) != 64 or not candidate.isascii() or any(
            character not in "0123456789abcdef" for character in candidate.lower()
        ):
            return "INVALID"
        return candidate
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "INVALID"


def _previous_hash(path: Path) -> str:
    """Resume the chain from the active file, or the newest segment if it is empty."""
    if path.exists() and path.stat().st_size:
        return _last_hash(path)
    previous_segments = segments(path)
    return _last_hash(previous_segments[-1]) if previous_segments else GENESIS


def record(
    event_type: str,
    *,
    actor: str,
    tenant_id: str = "default",
    session_id: str = "",
    details: dict | None = None,
    path: Path | None = None,
) -> dict:
    path = path or AUDIT_PATH
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        previous = _previous_hash(path)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "actor": actor,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "details": redact(details or {}),
            "previous_hash": previous,
        }
        event["hash"] = hashlib.sha256(previous.encode("ascii") + _canonical(event)).hexdigest()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event


def _iter_events(path: Path):
    """Stream one file. Never materialises the whole log."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"event_type": "invalid_json", "raw": "[INVALID]"}


def read_events(limit: int = 100, path: Path | None = None) -> list[dict]:
    """The newest `limit` events, reading back only as many segments as needed."""
    path = path or AUDIT_PATH
    limit = max(0, limit)
    if not limit:
        return []
    collected: deque = deque(maxlen=limit)
    # Walk back from the active file through newer segments, stopping as soon
    # as enough events have been collected.
    for source in [path] + list(reversed(segments(path))):
        chunk: deque = deque(_iter_events(source), maxlen=limit)
        collected.extendleft(reversed(chunk))
        if len(collected) >= limit:
            break
    return list(collected)


def verify(path: Path | None = None, *, all_segments: bool = True) -> tuple[bool, int, str]:
    """Verify the hash chain. By default it spans every segment."""
    path = path or AUDIT_PATH
    sources = (segments(path) + [path]) if all_segments else [path]
    previous = GENESIS
    count = 0
    for source in sources:
        for event in _iter_events(source):
            count += 1
            supplied = event.pop("hash", None)
            if event.get("previous_hash") != previous:
                return False, count, "previous hash mismatch"
            calculated = hashlib.sha256(previous.encode("ascii") + _canonical(event)).hexdigest()
            if not hmac_compare(supplied, calculated):
                return False, count, "event hash mismatch"
            previous = calculated
    return True, count, previous


def hmac_compare(left: object, right: str) -> bool:
    return isinstance(left, str) and left.isascii() and hmac.compare_digest(left, right)
