"""/memory routes — durable per-caller memory for the CrisisLine voice agent.

Contract consumed by the voice bot (crisis_memory.py MemoryClient):
  GET  /memory/{caller_id}            -> caller record, or 404 if unknown
  POST /memory/{caller_id}/session    -> store a completed call's record
  POST /memory/{caller_id}/followup   -> record a promised follow-up

Extra read/admin routes for the dashboard and the follow-up scheduler:
  GET   /memory/                      -> list callers
  GET   /memory/followups/all         -> list follow-ups (optional ?status=)
  PATCH /memory/followups/{id}        -> update a follow-up (status/due_at)
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import memory_db
from app.service.callback_service import parse_when

memory_router = APIRouter()


class FollowupIn(BaseModel):
    when: str
    focus: str
    status: str = "scheduled"
    due_at: str | None = None


class FollowupPatch(BaseModel):
    status: str | None = None
    due_at: str | None = None
    focus: str | None = None


@memory_router.get("/{caller_id}")
def get_caller(caller_id: str) -> dict[str, Any]:
    """Return a caller's full record (profile + sessions + followups)."""
    record = memory_db.get_caller(caller_id)
    if record is None:
        raise HTTPException(status_code=404, detail="caller not found")
    return record


@memory_router.post("/{caller_id}/session")
def save_session(caller_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Persist a completed call's structured record (condition, risk, what helped, transcript)."""
    session_id = memory_db.add_session(caller_id, record)
    return {"ok": True, "session_id": session_id}


@memory_router.post("/{caller_id}/followup")
def save_followup(caller_id: str, body: FollowupIn) -> dict[str, Any]:
    """Record a promised follow-up check-in. Resolve a concrete due_at from the
    caller's natural-language 'when' so the scheduler can fire it."""
    due_at = body.due_at or parse_when(body.when)
    followup_id = memory_db.add_followup(
        caller_id,
        when_text=body.when,
        focus=body.focus,
        due_at=due_at,
        status=body.status,
    )
    return {"ok": True, "followup_id": followup_id, "due_at": due_at}


@memory_router.get("/")
def list_callers() -> dict[str, Any]:
    """List all known callers (dashboard)."""
    return {"callers": memory_db.list_callers()}


@memory_router.get("/followups/all")
def list_followups(status: str | None = None) -> dict[str, Any]:
    """List follow-ups, optionally filtered by status (scheduler uses ?status=scheduled)."""
    return {"followups": memory_db.list_followups(status)}


@memory_router.patch("/followups/{followup_id}")
def patch_followup(followup_id: int, body: FollowupPatch) -> dict[str, Any]:
    """Update a follow-up (e.g. mark placed/done, set due_at)."""
    changed = memory_db.update_followup(
        followup_id, status=body.status, due_at=body.due_at, focus=body.focus
    )
    if not changed:
        raise HTTPException(status_code=404, detail="followup not found or nothing to update")
    return {"ok": True}
