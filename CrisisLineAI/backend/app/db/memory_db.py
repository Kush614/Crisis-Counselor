"""SQLite-backed per-caller memory for the CrisisLine voice agent.

This is the durable store behind the agent's cross-call memory and follow-up
callbacks. The voice bot (yc-voice-agents-hackathon-main/server/crisis_memory.py)
reads/writes it over HTTP via the /memory routes; the counselor dashboard and the
follow-up scheduler read from the same tables.

Implementation notes:
  * Stdlib ``sqlite3`` (no extra dependency). One module-level connection with
    ``check_same_thread=False`` guarded by a lock — fine for the hackathon's
    concurrency and even modest production. Each session's full structured record
    is kept as JSON in ``sessions.data``; hot fields are also promoted to columns
    for cheap lookups and dashboard queries.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.getenv("MEMORY_DB_PATH", os.path.join(os.getcwd(), "crisisline_memory.db"))

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    """Create the connection and tables if they don't exist. Idempotent."""
    global _conn
    if _conn is not None:
        return
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS callers (
                caller_id       TEXT PRIMARY KEY,
                name            TEXT,
                first_seen      TEXT,
                last_seen       TEXT,
                last_condition  TEXT,
                last_risk_level TEXT,
                last_summary    TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                condition   TEXT,
                risk_level  TEXT,
                summary     TEXT,
                data        TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS followups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                due_at      TEXT,
                when_text   TEXT,
                focus       TEXT,
                status      TEXT NOT NULL DEFAULT 'scheduled'
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_caller ON sessions(caller_id);
            CREATE INDEX IF NOT EXISTS idx_followups_caller ON followups(caller_id);
            CREATE INDEX IF NOT EXISTS idx_followups_status ON followups(status);
            """
        )
        _conn.commit()


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# --- caller upsert ---------------------------------------------------------

def _upsert_caller(caller_id: str, **fields: Any) -> None:
    conn = _require_conn()
    now = _now()
    row = conn.execute("SELECT caller_id FROM callers WHERE caller_id = ?", (caller_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO callers (caller_id, first_seen, last_seen) VALUES (?, ?, ?)",
            (caller_id, now, now),
        )
    sets = ["last_seen = ?"]
    vals: list[Any] = [now]
    for k, v in fields.items():
        if v is not None:
            sets.append(f"{k} = ?")
            vals.append(v)
    vals.append(caller_id)
    conn.execute(f"UPDATE callers SET {', '.join(sets)} WHERE caller_id = ?", vals)


# --- reads -----------------------------------------------------------------

def get_caller(caller_id: str) -> dict[str, Any] | None:
    """Full caller record (profile + sessions + followups), or None if unknown.

    Session objects are the original JSON the bot stored, so consumers like
    build_caller_context() see condition / summary / what_helped directly.
    """
    with _lock:
        conn = _require_conn()
        caller = conn.execute(
            "SELECT * FROM callers WHERE caller_id = ?", (caller_id,)
        ).fetchone()
        if caller is None:
            return None
        sessions = conn.execute(
            "SELECT data FROM sessions WHERE caller_id = ? ORDER BY id ASC", (caller_id,)
        ).fetchall()
        followups = conn.execute(
            "SELECT id, due_at, when_text, focus, status, created_at "
            "FROM followups WHERE caller_id = ? ORDER BY id ASC",
            (caller_id,),
        ).fetchall()
    record = dict(caller)
    record["sessions"] = [json.loads(s["data"]) for s in sessions]
    record["followups"] = [
        {
            "id": f["id"],
            "due_at": f["due_at"],
            "when": f["when_text"],
            "focus": f["focus"],
            "status": f["status"],
            "created_at": f["created_at"],
        }
        for f in followups
    ]
    return record


def list_callers() -> list[dict[str, Any]]:
    """Lightweight list of callers for the dashboard."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            "SELECT caller_id, name, last_seen, last_condition, last_risk_level, last_summary "
            "FROM callers ORDER BY last_seen DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def list_followups(status: str | None = None) -> list[dict[str, Any]]:
    """Follow-ups across all callers, optionally filtered by status (for the scheduler)."""
    with _lock:
        conn = _require_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM followups WHERE status = ? ORDER BY id ASC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM followups ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


# --- writes ----------------------------------------------------------------

def add_session(caller_id: str, record: dict[str, Any]) -> int:
    """Persist a completed call's structured record; update the caller's last_* fields."""
    condition = record.get("condition")
    risk_level = record.get("risk_level")
    summary = record.get("summary")
    if isinstance(summary, (dict, list)):
        summary = json.dumps(summary)
    with _lock:
        conn = _require_conn()
        cur = conn.execute(
            "INSERT INTO sessions (caller_id, created_at, condition, risk_level, summary, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (caller_id, _now(), condition, risk_level, summary, json.dumps(record, default=str)),
        )
        _upsert_caller(
            caller_id,
            last_condition=condition,
            last_risk_level=risk_level,
            last_summary=summary,
        )
        conn.commit()
        return int(cur.lastrowid)


def add_followup(
    caller_id: str,
    when_text: str,
    focus: str,
    due_at: str | None = None,
    status: str = "scheduled",
) -> int:
    """Record a promised follow-up. ``due_at`` (ISO) is set by the scheduler step."""
    with _lock:
        conn = _require_conn()
        cur = conn.execute(
            "INSERT INTO followups (caller_id, created_at, due_at, when_text, focus, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (caller_id, _now(), due_at, when_text, focus, status),
        )
        _upsert_caller(caller_id)
        conn.commit()
        return int(cur.lastrowid)


def update_followup(followup_id: int, **fields: Any) -> bool:
    """Update a follow-up (e.g. status -> placed/done, or set due_at). Returns True if a row changed."""
    allowed = {"status", "due_at", "focus", "when_text"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return False
    with _lock:
        conn = _require_conn()
        cols = ", ".join(f"{k} = ?" for k in sets)
        cur = conn.execute(
            f"UPDATE followups SET {cols} WHERE id = ?", (*sets.values(), followup_id)
        )
        conn.commit()
        return cur.rowcount > 0
