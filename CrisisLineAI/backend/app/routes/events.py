"""Live event stream powering the real-time 'agent mind' dashboard.

The voice agent POSTs an event each time it reads the caller (condition, risk,
safety check, what helped, escalation, follow-up, call start). The dashboard
polls /events/recent and renders the agent's reasoning live. In-memory ring
buffer — demo-grade, resets on restart.
"""

import time
from collections import deque

from fastapi import APIRouter, Request

events_router = APIRouter()
_EVENTS: deque = deque(maxlen=400)


@events_router.post("/events")
async def post_event(req: Request) -> dict:
    """Agent posts a reasoning event: {caller_id, kind, label, value}."""
    try:
        e = await req.json()
    except Exception:
        e = {}
    e["ts"] = time.time()
    _EVENTS.append(e)
    return {"ok": True}


@events_router.get("/events/recent")
def recent(since: float = 0.0) -> dict:
    """Dashboard polls this; pass ?since=<last ts> to get only new events."""
    return {"now": time.time(), "events": [e for e in _EVENTS if e.get("ts", 0) > since]}


@events_router.post("/events/clear")
def clear() -> dict:
    """Reset the feed (handy right before a demo)."""
    _EVENTS.clear()
    return {"ok": True}
