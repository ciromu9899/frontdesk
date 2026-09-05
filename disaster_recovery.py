"""Create and verify a recoverable SQLite backup."""
from __future__ import annotations
import argparse, json, sqlite3
from contextlib import closing
from pathlib import Path
import state

def backup(destination: Path, source: Path | None = None) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(state.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        # Table identifiers come only from this closed tuple, never from input.
        counts = {table: dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
                  for table in ("sessions", "deliveries", "tenant_documents", "privacy_requests",
                                "conversation_threads", "conversation_messages", "csat_responses",
                                "metric_events")}
    if integrity != "ok": raise RuntimeError(f"backup integrity failed: {integrity}")
    return {"integrity": integrity, **counts}

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("destination", type=Path); args=parser.parse_args()
    print(json.dumps(backup(args.destination), indent=2)); return 0
if __name__ == "__main__": raise SystemExit(main())
