"""Lightweight follow-up scheduler — a plain asyncio poller (no APScheduler).

Every CRISIS_SCHEDULER_INTERVAL seconds it looks for 'scheduled' follow-ups whose
due_at has passed, and places an outbound callback for each, marking it 'placed'.
Runs as a background task started in main.py's startup. The blocking Twilio REST
call is pushed to a thread so it never stalls the event loop.

Caller IDs that aren't phone numbers (e.g. 'webrtc-local' from browser testing)
are marked 'skipped' — you can't call back a browser.
"""

import asyncio
import os
from datetime import datetime, timezone

from app.db import memory_db
from app.service.callback_service import place_outbound_call

POLL_INTERVAL = int(os.getenv("CRISIS_SCHEDULER_INTERVAL", "15"))
ENABLED = os.getenv("CRISIS_SCHEDULER_ENABLED", "true").lower() == "true"


async def place_due_callback(followup: dict) -> str:
    """Place one outbound callback (offloaded to a thread). Returns the call SID."""
    to = followup["caller_id"]
    return await asyncio.get_event_loop().run_in_executor(None, place_outbound_call, to)


async def tick() -> int:
    """Place any due callbacks. Returns how many were placed. Safe to call manually."""
    now = datetime.now(timezone.utc).isoformat()
    placed = 0
    for f in memory_db.list_followups("scheduled"):
        due_at = f.get("due_at")
        if not due_at or due_at > now:  # not due yet (ISO strings compare lexicographically)
            continue
        caller_id = f.get("caller_id", "")
        if not caller_id.startswith("+"):
            memory_db.update_followup(f["id"], status="skipped")
            print(f"[scheduler] follow-up {f['id']} skipped — '{caller_id}' is not a phone number")
            continue
        try:
            sid = await place_due_callback(f)
            memory_db.update_followup(f["id"], status="placed")
            placed += 1
            print(f"[scheduler] placed callback to {caller_id} for follow-up {f['id']} (sid={sid})")
        except Exception as e:
            memory_db.update_followup(f["id"], status="error")
            print(f"[scheduler] follow-up {f['id']} errored (non-fatal): {e}")
    return placed


async def run_scheduler() -> None:
    """Background loop. Started via asyncio.create_task in startup."""
    if not ENABLED:
        print("[scheduler] disabled (CRISIS_SCHEDULER_ENABLED=false)")
        return
    print(f"[scheduler] running — polling every {POLL_INTERVAL}s")
    while True:
        try:
            await tick()
        except Exception as e:  # a bad row must not kill the loop
            print(f"[scheduler] tick error (continuing): {e}")
        await asyncio.sleep(POLL_INTERVAL)
