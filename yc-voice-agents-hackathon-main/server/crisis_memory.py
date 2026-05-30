#
# CrisisLine — counselor voice agent
#

"""Per-caller memory client for the CrisisLine voice agent.

Two backends, chosen automatically:
  * HTTP — if MEMORY_API_URL is set, talk to the CrisisLine FastAPI backend
    (GET /memory/{caller_id}, POST /memory/{caller_id}/session, POST
    /memory/{caller_id}/followup). This is the real path used in the demo.
  * Local JSON — otherwise, persist to a local file (MEMORY_LOCAL_PATH, default
    .crisis_memory.json) so the bot has working cross-call memory standalone,
    before the backend endpoints exist.

Every method is defensive: a memory failure must never crash a live call. On
error we log and degrade (load returns None; saves are best-effort).
"""

import json
import os
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

MEMORY_API_URL = os.getenv("MEMORY_API_URL")  # e.g. http://localhost:1000
LOCAL_STORE = Path(os.getenv("MEMORY_LOCAL_PATH", ".crisis_memory.json"))


class MemoryClient:
    """Loads a caller's history at call start; writes session + follow-ups at end."""

    def __init__(self) -> None:
        self.api_url = MEMORY_API_URL.rstrip("/") if MEMORY_API_URL else None
        if self.api_url:
            logger.info(f"Memory: HTTP backend at {self.api_url}")
        else:
            logger.info(f"Memory: local JSON store at {LOCAL_STORE.resolve()}")

    # --- public API --------------------------------------------------------

    async def load_caller(self, caller_id: str) -> dict[str, Any] | None:
        """Return the caller's stored profile, or None if unknown / on error."""
        if not caller_id:
            return None
        try:
            if self.api_url:
                return await self._http_get(f"/memory/{caller_id}")
            return self._local_load().get(caller_id)
        except Exception as e:  # never let memory sink a call
            logger.warning(f"Memory load failed for {caller_id} (non-fatal): {e}")
            return None

    async def save_session(self, caller_id: str, record: dict[str, Any]) -> None:
        """Persist a completed session (notes, risk, condition, transcript)."""
        if not caller_id:
            return
        try:
            if self.api_url:
                await self._http_post(f"/memory/{caller_id}/session", record)
            else:
                self._local_save_session(caller_id, record)
            logger.info(f"Memory: saved session for {caller_id}")
        except Exception as e:
            logger.warning(f"Memory save failed for {caller_id} (non-fatal): {e}")

    def emit_event(self, caller_id: str, kind: str, label: str, value: str = "") -> None:
        """Fire-and-forget: push a live reasoning event to the dashboard feed.
        Non-blocking so it never adds latency to a live turn."""
        if not self.api_url:
            return

        async def _send():
            try:
                await self._http_post("/events", {
                    "caller_id": caller_id, "kind": kind, "label": label, "value": value,
                })
            except Exception:
                pass

        try:
            import asyncio
            asyncio.get_event_loop().create_task(_send())
        except Exception:
            pass

    async def schedule_followup(self, caller_id: str, when: str, focus: str) -> None:
        """Record a promised follow-up check-in for the scheduler to pick up."""
        if not caller_id:
            return
        payload = {"when": when, "focus": focus, "status": "scheduled"}
        try:
            if self.api_url:
                await self._http_post(f"/memory/{caller_id}/followup", payload)
            else:
                self._local_add_followup(caller_id, payload)
            logger.info(f"Memory: scheduled follow-up for {caller_id} — '{focus}' ({when})")
        except Exception as e:
            logger.warning(f"Memory follow-up save failed for {caller_id} (non-fatal): {e}")

    # --- HTTP backend ------------------------------------------------------

    async def _http_get(self, path: str) -> dict[str, Any] | None:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{self.api_url}{path}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 404:
                    return None
                r.raise_for_status()
                return await r.json()

    async def _http_post(self, path: str, body: dict[str, Any]) -> None:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{self.api_url}{path}", json=body, timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                r.raise_for_status()

    # --- local JSON backend ------------------------------------------------

    def _local_load(self) -> dict[str, Any]:
        if LOCAL_STORE.exists():
            return json.loads(LOCAL_STORE.read_text(encoding="utf-8"))
        return {}

    def _local_write(self, data: dict[str, Any]) -> None:
        LOCAL_STORE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _local_save_session(self, caller_id: str, record: dict[str, Any]) -> None:
        data = self._local_load()
        entry = data.setdefault(caller_id, {"caller_id": caller_id, "sessions": [], "followups": []})
        entry["sessions"].append(record)
        # Surface the latest read to the top level for cheap continuity lookups.
        entry["last_condition"] = record.get("condition")
        entry["last_risk_level"] = record.get("risk_level")
        entry["last_summary"] = record.get("summary")
        self._local_write(data)

    def _local_add_followup(self, caller_id: str, payload: dict[str, Any]) -> None:
        data = self._local_load()
        entry = data.setdefault(caller_id, {"caller_id": caller_id, "sessions": [], "followups": []})
        entry["followups"].append(payload)
        self._local_write(data)


def build_caller_context(record: dict[str, Any] | None) -> str:
    """Turn a stored caller record into a continuity instruction for the prompt.

    Deliberately conversational, not a data dump — the model should reference the
    past warmly, never recite it like a file.
    """
    if not record or not record.get("sessions"):
        return ""
    last = record["sessions"][-1]
    condition = last.get("condition") or record.get("last_condition")
    summary = last.get("summary") or record.get("last_summary")
    helped = last.get("what_helped") or []
    helped_str = ", ".join(
        h.get("technique", "") for h in helped if isinstance(h, dict)
    ).strip(", ")

    bits = ["This person has talked with you before."]
    if summary:
        bits.append(f"Last time, in short: {summary}")
    if condition:
        bits.append(f"You read their state as {condition}.")
    if helped_str:
        bits.append(f"What seemed to help: {helped_str}.")
    bits.append(
        "Open with warm continuity — gently reference what they were carrying last time and ask "
        "how they've been since, e.g. \"it's good to hear your voice again — last time things felt "
        "really heavy, how have you been holding up?\" Never recite their details back like a record."
    )
    return " ".join(bits)


def build_followup_context(record: dict[str, Any] | None) -> str:
    """If there's a pending follow-up, return the opener instruction for an
    outbound check-in call we initiated. Empty string if none pending."""
    if not record:
        return ""
    pending = [f for f in record.get("followups", []) if f.get("status") == "scheduled"]
    if not pending:
        return ""
    focus = pending[-1].get("focus", "how they're doing")
    return (
        f"You promised to check in about: {focus}. You are calling THEM now to keep that promise. "
        "Open gently — remind them you said you'd check in, and ask how they're doing today. "
        "Do NOT assume they're in crisis; this is a caring follow-up, not an emergency."
    )
