"""Active system prompt store — lets the auto-improve loop swap the agent's
prompt WITHOUT a redeploy.

The deployed bot GETs /active_prompt at the start of every call and uses it as
its base instruction. The optimizer POSTs an improved prompt between rounds, so
the next Cekura run exercises the new prompt instantly. In-memory; falls back to
the bot's baked-in BASE_SYSTEM when empty.
"""

from fastapi import APIRouter, Request

prompt_router = APIRouter()
_STATE: dict = {"prompt": ""}


@prompt_router.get("/active_prompt")
def get_active_prompt() -> dict:
    return {"prompt": _STATE["prompt"]}


@prompt_router.post("/active_prompt")
async def set_active_prompt(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:
        body = {}
    _STATE["prompt"] = (body.get("prompt") or "").strip()
    return {"ok": True, "chars": len(_STATE["prompt"])}
