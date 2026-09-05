"""SQLite-backed durable state with tenant-scoped primary keys."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from config import DATA_DIR


ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = DATA_DIR / "frontdesk.db"
SCHEMA_VERSION = 2
_LOCK = threading.RLock()


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def path() -> Path:
    configured = os.environ.get("FRONTDESK_STATE_DB", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_PATH


def connect(database: Path | None = None) -> sqlite3.Connection:
    target = database or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10, isolation_level=None,
                                 factory=_Connection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    with _LOCK:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            tenant_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            principal_json TEXT NOT NULL,
            history_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, conversation_key)
        );
        CREATE TABLE IF NOT EXISTS deliveries (
            tenant_id TEXT NOT NULL,
            delivery_key TEXT NOT NULL,
            seen_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, delivery_key)
        );
        CREATE TABLE IF NOT EXISTS tenant_documents (
            tenant_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            document_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, namespace, document_key)
        );
        CREATE TABLE IF NOT EXISTS privacy_requests (
            tenant_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            completed_at REAL,
            PRIMARY KEY (tenant_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS conversation_threads (
            tenant_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'web',
            customer_id TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            assignee TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_message_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, conversation_key)
        );
        CREATE INDEX IF NOT EXISTS idx_threads_tenant_updated
            ON conversation_threads (tenant_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS conversation_messages (
            tenant_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            message_id TEXT NOT NULL,
            sender_kind TEXT NOT NULL,
            sender_id TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'web',
            created_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, conversation_key, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_thread
            ON conversation_messages (tenant_id, conversation_key, created_at);
        CREATE TABLE IF NOT EXISTS csat_responses (
            tenant_id TEXT NOT NULL,
            response_id TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, response_id)
        );
        CREATE INDEX IF NOT EXISTS idx_csat_tenant_created
            ON csat_responses (tenant_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS metric_events (
            tenant_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            conversation_key TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL DEFAULT 1,
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_tenant_type
            ON metric_events (tenant_id, event_type, created_at DESC);
        """)
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return connection


def save_session(tenant_id: str, conversation_key: str, payload: dict,
                 database: Path | None = None) -> None:
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,conversation_key) DO UPDATE SET "
            "session_id=excluded.session_id,principal_json=excluded.principal_json,"
            "history_json=excluded.history_json,updated_at=excluded.updated_at",
            (tenant_id, conversation_key, payload["session_id"],
             json.dumps(payload["principal"], separators=(",", ":")),
             json.dumps(payload["history"], ensure_ascii=False, separators=(",", ":")),
             time.time()),
        )


def load_session(tenant_id: str, conversation_key: str,
                 database: Path | None = None) -> dict | None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM sessions WHERE tenant_id=? AND conversation_key=?",
            (tenant_id, conversation_key),
        ).fetchone()
    if row is None:
        return None
    return {"session_id": row["session_id"],
            "principal": json.loads(row["principal_json"]),
            "history": json.loads(row["history_json"])}


def already_seen(tenant_id: str, delivery_key: str, *, ttl: float,
                 now: float | None = None, database: Path | None = None) -> bool:
    if not delivery_key:
        return False
    current = time.time() if now is None else now
    with _LOCK, connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM deliveries WHERE seen_at < ?", (current - ttl,))
        row = connection.execute(
            "SELECT 1 FROM deliveries WHERE tenant_id=? AND delivery_key=?",
            (tenant_id, delivery_key),
        ).fetchone()
        if row is None:
            connection.execute("INSERT INTO deliveries VALUES(?,?,?)",
                               (tenant_id, delivery_key, current))
        connection.execute("COMMIT")
    return row is not None


def delivery_seen(tenant_id: str, delivery_key: str, *, ttl: float,
                  now: float | None = None, database: Path | None = None) -> bool:
    """Check a delivery without claiming it, for transports that retry on failure."""
    if not delivery_key:
        return False
    current = time.time() if now is None else now
    with connect(database) as connection:
        row = connection.execute(
            """SELECT 1 FROM deliveries
               WHERE tenant_id=? AND delivery_key=? AND seen_at>=?""",
            (tenant_id, delivery_key, current - ttl)).fetchone()
    return row is not None


def mark_delivery(tenant_id: str, delivery_key: str, *,
                  now: float | None = None, database: Path | None = None) -> None:
    """Mark a transport delivery complete after its observable side effect succeeds."""
    if not delivery_key:
        return
    current = time.time() if now is None else now
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO deliveries VALUES(?,?,?)
               ON CONFLICT(tenant_id,delivery_key) DO UPDATE SET seen_at=excluded.seen_at""",
            (tenant_id, delivery_key, current))


def reset_deliveries(database: Path | None = None) -> None:
    with connect(database) as connection:
        connection.execute("DELETE FROM deliveries")


def get_document(tenant_id: str, namespace: str, key: str,
                 database: Path | None = None) -> dict | None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT value_json FROM tenant_documents WHERE tenant_id=? AND namespace=? AND document_key=?",
            (tenant_id, namespace, key),
        ).fetchone()
    return json.loads(row[0]) if row else None


def put_document(tenant_id: str, namespace: str, key: str, value: dict,
                 database: Path | None = None) -> None:
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO tenant_documents VALUES(?,?,?,?,?) "
            "ON CONFLICT(tenant_id,namespace,document_key) DO UPDATE SET "
            "value_json=excluded.value_json,updated_at=excluded.updated_at",
            (tenant_id, namespace, key, json.dumps(value, ensure_ascii=False), time.time()),
        )


def update_document(tenant_id: str, namespace: str, key: str,
                    mutate, default: dict | None = None,
                    database: Path | None = None):
    """Read, change and write one document without losing a concurrent change.

    `put_document` writes a whole document, so two callers that each read, add
    their own row and write back leave only the second one's work: a customer is
    told an appointment is confirmed and the record of it is gone. Everything
    that decides on what it just read has to happen inside one transaction, so
    the caller passes the change rather than performing it around this function.

    `mutate(document)` may raise to abort the write, and whatever it returns is
    returned to the caller with the write already committed.
    """
    with _LOCK, connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value_json FROM tenant_documents "
            "WHERE tenant_id=? AND namespace=? AND document_key=?",
            (tenant_id, namespace, key),
        ).fetchone()
        document = json.loads(row[0]) if row else (
            json.loads(json.dumps(default)) if default is not None else {})
        try:
            outcome = mutate(document)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute(
            "INSERT INTO tenant_documents VALUES(?,?,?,?,?) "
            "ON CONFLICT(tenant_id,namespace,document_key) DO UPDATE SET "
            "value_json=excluded.value_json,updated_at=excluded.updated_at",
            (tenant_id, namespace, key,
             json.dumps(document, ensure_ascii=False), time.time()),
        )
        connection.execute("COMMIT")
    return outcome


def delete_subject(tenant_id: str, subject: str,
                   database: Path | None = None) -> dict[str, int]:
    """Atomically delete one subject's sessions, inbox transcripts, CSAT, and metrics."""
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT conversation_key,principal_json FROM sessions WHERE tenant_id=?",
            (tenant_id,),
        ).fetchall()
        keys = [row["conversation_key"] for row in rows
                if json.loads(row["principal_json"]).get("subject") == subject]
        thread_keys = {row["conversation_key"] for row in connection.execute(
            "SELECT conversation_key FROM conversation_threads WHERE tenant_id=? AND customer_id=?",
            (tenant_id, subject)).fetchall()}
        thread_keys.update(row["conversation_key"] for row in connection.execute(
            "SELECT DISTINCT conversation_key FROM conversation_messages WHERE tenant_id=? AND sender_id=?",
            (tenant_id, subject)).fetchall())
        for key in keys:
            connection.execute("DELETE FROM sessions WHERE tenant_id=? AND conversation_key=?",
                               (tenant_id, key))
        counts = {"sessions": len(keys)}
        for key in thread_keys:
            counts["messages"] = counts.get("messages", 0) + connection.execute(
                "DELETE FROM conversation_messages WHERE tenant_id=? AND conversation_key=?",
                (tenant_id, key)).rowcount
            counts["csat"] = counts.get("csat", 0) + connection.execute(
                "DELETE FROM csat_responses WHERE tenant_id=? AND conversation_key=?",
                (tenant_id, key)).rowcount
            counts["metrics"] = counts.get("metrics", 0) + connection.execute(
                "DELETE FROM metric_events WHERE tenant_id=? AND conversation_key=?",
                (tenant_id, key)).rowcount
            counts["threads"] = counts.get("threads", 0) + connection.execute(
                "DELETE FROM conversation_threads WHERE tenant_id=? AND conversation_key=?",
                (tenant_id, key)).rowcount
        connection.execute("COMMIT")
    return {key: value for key, value in counts.items() if value or key == "sessions"}


def export_subject(tenant_id: str, subject: str,
                   database: Path | None = None) -> dict:
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT * FROM sessions WHERE tenant_id=? ORDER BY updated_at",
            (tenant_id,),
        ).fetchall()
        thread_rows = connection.execute(
            "SELECT * FROM conversation_threads WHERE tenant_id=? AND customer_id=? ORDER BY created_at",
            (tenant_id, subject)).fetchall()
        thread_keys = [row["conversation_key"] for row in thread_rows]
        messages = []
        csat = []
        for key in thread_keys:
            messages.extend(dict(row) for row in connection.execute(
                "SELECT * FROM conversation_messages WHERE tenant_id=? AND conversation_key=? ORDER BY created_at",
                (tenant_id, key)).fetchall())
            csat.extend(dict(row) for row in connection.execute(
                "SELECT * FROM csat_responses WHERE tenant_id=? AND conversation_key=? ORDER BY created_at",
                (tenant_id, key)).fetchall())
    sessions = []
    for row in rows:
        principal = json.loads(row["principal_json"])
        if principal.get("subject") == subject:
            sessions.append({"conversation_key": row["conversation_key"],
                             "session_id": row["session_id"],
                             "history": json.loads(row["history_json"]),
                             "updated_at": row["updated_at"]})
    return {"tenant_id": tenant_id, "subject": subject, "sessions": sessions,
            "threads": [dict(row) for row in thread_rows], "messages": messages,
            "csat": csat}


def create_privacy_request(tenant_id: str, request_type: str, subject: str,
                           database: Path | None = None) -> dict:
    request_id = f"P-{uuid.uuid4().hex[:12].upper()}"; created = time.time()
    with connect(database) as connection:
        connection.execute("INSERT INTO privacy_requests VALUES(?,?,?,?,?,?,NULL)",
                           (tenant_id, request_id, request_type, subject, "pending", created))
    return {"request_id": request_id, "tenant_id": tenant_id,
            "request_type": request_type, "subject": subject,
            "status": "pending", "created_at": created}


def complete_privacy_request(tenant_id: str, request_id: str,
                             database: Path | None = None) -> bool:
    with connect(database) as connection:
        cursor = connection.execute(
            "UPDATE privacy_requests SET status='completed',completed_at=? "
            "WHERE tenant_id=? AND request_id=? AND status='pending'",
            (time.time(), tenant_id, request_id))
    return cursor.rowcount == 1


def complete_subject_deletion(tenant_id: str, subject: str, request_id: str,
                              database: Path | None = None) -> dict[str, int]:
    """Atomically validate a deletion request, erase sessions, and complete it."""
    with connect(database) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT 1 FROM privacy_requests WHERE tenant_id=? AND request_id=? "
                "AND request_type='delete' AND subject=? AND status='pending'",
                (tenant_id, request_id, subject),
            ).fetchone()
            if request is None:
                raise ValueError("pending deletion request not found")
            rows = connection.execute(
                "SELECT conversation_key,principal_json FROM sessions WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
            keys = [row["conversation_key"] for row in rows
                    if json.loads(row["principal_json"]).get("subject") == subject]
            for key in keys:
                connection.execute(
                    "DELETE FROM sessions WHERE tenant_id=? AND conversation_key=?",
                    (tenant_id, key),
                )
            connection.execute(
                "UPDATE privacy_requests SET status='completed',completed_at=? "
                "WHERE tenant_id=? AND request_id=?",
                (time.time(), tenant_id, request_id),
            )
            connection.execute("COMMIT")
            return {"sessions": len(keys)}
        except Exception:
            connection.execute("ROLLBACK")
            raise


def status(database: Path | None = None) -> dict:
    with connect(database) as connection:
        # Table identifiers come only from this closed tuple, never from input.
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
                  for table in ("sessions", "deliveries", "tenant_documents", "privacy_requests",
                                "conversation_threads", "conversation_messages", "csat_responses")}
    return {"path": str(database or path()), "schema_version": SCHEMA_VERSION, **counts}


def upsert_thread(tenant_id: str, conversation_key: str, *, channel: str = "web",
                  customer_id: str = "", subject: str = "", status_value: str | None = None,
                  assignee: str | None = None, priority: str | None = None,
                  metadata: dict | None = None, database: Path | None = None,
                  now: float | None = None) -> dict:
    """Create or update one tenant-scoped shared-inbox thread."""
    timestamp = time.time() if now is None else now
    with connect(database) as connection:
        existing = connection.execute(
            "SELECT * FROM conversation_threads WHERE tenant_id=? AND conversation_key=?",
            (tenant_id, conversation_key)).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO conversation_threads
                   (tenant_id, conversation_key, channel, customer_id, subject, status,
                    assignee, priority, created_at, updated_at, last_message_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tenant_id, conversation_key, channel, customer_id, subject,
                 status_value or "open", assignee or "", priority or "normal",
                 timestamp, timestamp, timestamp, json.dumps(metadata or {})))
        else:
            current_metadata = json.loads(existing["metadata_json"])
            connection.execute(
                """UPDATE conversation_threads SET channel=?, customer_id=?, subject=?,
                   status=?, assignee=?, priority=?, updated_at=?, metadata_json=?
                   WHERE tenant_id=? AND conversation_key=?""",
                (channel or existing["channel"], customer_id or existing["customer_id"],
                 subject or existing["subject"], status_value or existing["status"],
                 existing["assignee"] if assignee is None else assignee,
                 existing["priority"] if priority is None else priority, timestamp,
                 json.dumps(metadata if metadata is not None else current_metadata),
                 tenant_id, conversation_key))
    return get_thread(tenant_id, conversation_key, database=database) or {}


def append_message(tenant_id: str, conversation_key: str, sender_kind: str, body: str,
                   *, sender_id: str = "", channel: str = "web", metadata: dict | None = None,
                   message_id: str | None = None, database: Path | None = None,
                   now: float | None = None) -> dict:
    timestamp = time.time() if now is None else now
    message_id = message_id or uuid.uuid4().hex
    upsert_thread(tenant_id, conversation_key, channel=channel, customer_id=sender_id,
                  database=database, now=timestamp)
    with connect(database) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO conversation_messages
               (tenant_id, conversation_key, message_id, sender_kind, sender_id, body,
                channel, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tenant_id, conversation_key, message_id, sender_kind, sender_id, body,
             channel, timestamp, json.dumps(metadata or {}, ensure_ascii=False)))
        connection.execute(
            "UPDATE conversation_threads SET updated_at=?, last_message_at=? WHERE tenant_id=? AND conversation_key=?",
            (timestamp, timestamp, tenant_id, conversation_key))
    return {"message_id": message_id, "created_at": timestamp}


def list_threads(tenant_id: str, *, status_value: str = "", limit: int = 100,
                 database: Path | None = None) -> list[dict]:
    query = "SELECT * FROM conversation_threads WHERE tenant_id=?"
    values: list[object] = [tenant_id]
    if status_value:
        query += " AND status=?"
        values.append(status_value)
    query += " ORDER BY updated_at DESC LIMIT ?"
    values.append(max(1, min(limit, 500)))
    with connect(database) as connection:
        return [dict(row) for row in connection.execute(query, values).fetchall()]


def get_thread(tenant_id: str, conversation_key: str, *,
               database: Path | None = None) -> dict | None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM conversation_threads WHERE tenant_id=? AND conversation_key=?",
            (tenant_id, conversation_key)).fetchone()
        return dict(row) if row else None


def list_messages(tenant_id: str, conversation_key: str, *, limit: int = 200,
                  database: Path | None = None) -> list[dict]:
    with connect(database) as connection:
        rows = connection.execute(
            """SELECT * FROM conversation_messages WHERE tenant_id=? AND conversation_key=?
               ORDER BY created_at ASC LIMIT ?""",
            (tenant_id, conversation_key, max(1, min(limit, 1000)))).fetchall()
        return [dict(row) for row in rows]


def record_metric(tenant_id: str, event_type: str, *, conversation_key: str = "",
                  value: float = 1, dimensions: dict | None = None,
                  database: Path | None = None, now: float | None = None) -> str:
    event_id = uuid.uuid4().hex
    timestamp = time.time() if now is None else now
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO metric_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, event_id, event_type, conversation_key, value,
             json.dumps(dimensions or {}, ensure_ascii=False), timestamp))
    return event_id


def record_csat(tenant_id: str, conversation_key: str, rating: int, comment: str = "",
                *, database: Path | None = None, now: float | None = None) -> dict:
    if rating not in range(1, 6):
        raise ValueError("rating must be between 1 and 5")
    timestamp = time.time() if now is None else now
    with _LOCK, connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT response_id FROM csat_responses
               WHERE tenant_id=? AND conversation_key=?
               ORDER BY created_at DESC LIMIT 1""",
            (tenant_id, conversation_key)).fetchone()
        if existing is None:
            response_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO csat_responses VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, response_id, conversation_key, rating,
                 comment[:2000], timestamp))
            event_type = "csat_response"
        else:
            response_id = str(existing["response_id"])
            connection.execute(
                """UPDATE csat_responses SET rating=?, comment=?, created_at=?
                   WHERE tenant_id=? AND response_id=?""",
                (rating, comment[:2000], timestamp, tenant_id, response_id))
            event_type = "csat_updated"
        connection.execute("COMMIT")
    record_metric(tenant_id, event_type, conversation_key=conversation_key,
                  value=rating, database=database, now=timestamp)
    return {"response_id": response_id, "rating": rating, "created_at": timestamp,
            "updated": existing is not None}


def analytics(tenant_id: str, *, database: Path | None = None) -> dict:
    with connect(database) as connection:
        statuses = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status, COUNT(*) count FROM conversation_threads WHERE tenant_id=? GROUP BY status",
            (tenant_id,)).fetchall()}
        csat = connection.execute(
            "SELECT COUNT(*) count, AVG(rating) average FROM csat_responses WHERE tenant_id=?",
            (tenant_id,)).fetchone()
        events = {row["event_type"]: row["count"] for row in connection.execute(
            "SELECT event_type, COUNT(*) count FROM metric_events WHERE tenant_id=? GROUP BY event_type",
            (tenant_id,)).fetchall()}
        channels = {row["channel"]: row["count"] for row in connection.execute(
            "SELECT channel, COUNT(*) count FROM conversation_threads WHERE tenant_id=? GROUP BY channel",
            (tenant_id,)).fetchall()}
        latency = connection.execute(
            "SELECT AVG(value) average FROM metric_events WHERE tenant_id=? AND event_type='assistant_reply'",
            (tenant_id,)).fetchone()["average"]
    total_threads = sum(statuses.values())
    resolved = statuses.get("resolved", 0)
    return {"threads": statuses, "csat_responses": int(csat["count"]),
            "csat_average": round(float(csat["average"] or 0), 2), "events": events,
            "channels": channels,
            "average_assistant_seconds": round(float(latency or 0), 3),
            "resolution_rate": round(resolved / total_threads, 4) if total_threads else 0.0,
            "human_replies": int(events.get("human_reply", 0))}
