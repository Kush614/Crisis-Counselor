"""/callback routes — manual control over follow-up callbacks (demo + testing).

The scheduler places due callbacks automatically; these routes let you force one
now (a demo button) or place a one-off test call, without waiting for due_at.

  POST /callback/place/{followup_id}  -> place this follow-up's callback now
  POST /callback/test                 -> place a callback to an arbitrary number
  GET  /callback/due                  -> list scheduled follow-ups (debug)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import memory_db
from app.service.callback_service import place_outbound_call

callback_router = APIRouter()


class TestCall(BaseModel):
    to_number: str


@callback_router.post("/place/{followup_id}")
def place_followup(followup_id: int) -> dict:
    """Place the outbound callback for a specific follow-up immediately."""
    match = next((f for f in memory_db.list_followups() if f["id"] == followup_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="followup not found")
    caller_id = match.get("caller_id", "")
    if not caller_id.startswith("+"):
        raise HTTPException(status_code=400, detail=f"'{caller_id}' is not a callable phone number")
    try:
        sid = place_outbound_call(caller_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    memory_db.update_followup(followup_id, status="placed")
    return {"ok": True, "sid": sid, "to": caller_id}


@callback_router.post("/test")
def test_call(body: TestCall) -> dict:
    """Place a one-off outbound callback to any number (smoke-test the Twilio path)."""
    try:
        sid = place_outbound_call(body.to_number)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True, "sid": sid, "to": body.to_number}


@callback_router.get("/due")
def list_due() -> dict:
    """List scheduled follow-ups and their due times (debug)."""
    return {"scheduled": memory_db.list_followups("scheduled")}
