"""Text channels for the crisis counselor — SMS + iMessage — over one brain.

Both are inbound-text → LLM → reply-text. They share `crisis_reply`, which uses
the same 988 crisis Model (per-sender conversation memory keyed by phone number),
so a person texting gets continuity just like a caller.

  POST /sms        Twilio inbound-SMS webhook  -> returns TwiML <Message> reply
  POST /imessage   Sendblue inbound webhook     -> sends reply via Sendblue API

Voice (a real phone CALL) is the separate Pipecat bot (crisis-bot.py) + Twilio.

Setup per channel is in the response notes — both need a public URL (ngrok) so
the provider can reach these webhooks.
"""

import json
import os
import urllib.request
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.dependencies.model import get_model
from app.model.llm import Model

messaging_router = APIRouter()


# --- Voice: connect an inbound Twilio call to the deployed Pipecat agent ------

@messaging_router.api_route("/voice", methods=["GET", "POST"])
async def voice():
    """TwiML returned to Twilio on an inbound call — bridges the call's media
    stream to the deployed Pipecat Cloud agent (crisis-bot). Set this URL as the
    number's Voice webhook. PIPECAT_SERVICE_HOST = '<agent>.<namespace>'."""
    host = os.environ.get("PIPECAT_SERVICE_HOST", "")
    ws = os.environ.get("PIPECAT_TWILIO_WS", "wss://api.pipecat.daily.co/ws/twilio")
    twiml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Connect><Stream url='{ws}'>"
        f"<Parameter name='_pipecatCloudServiceHost' value='{host}'/>"
        "</Stream></Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


async def crisis_reply(text: str, session_id: str, model: Model) -> str:
    """Run one text turn through the 988 crisis Model with per-sender memory."""
    try:
        return await model.get_response(session_id=session_id, user_input=text)
    except Exception as e:  # never leave a texter hanging
        return (
            "I'm here with you. I'm having a brief technical hiccup, but you're not alone — "
            "if you're in immediate danger please call 988 or 911."
        ) if not os.getenv("CRISIS_DEBUG") else f"[error: {e}]"


# --- Twilio SMS -------------------------------------------------------------

@messaging_router.post("/sms")
async def sms(request: Request, model: Model = Depends(get_model)):
    """Twilio posts inbound SMS here (form-encoded). Reply is spoken back as TwiML."""
    form = await request.form()
    body = (form.get("Body") or "").strip()
    sender = form.get("From") or "sms-unknown"
    reply = await crisis_reply(body, f"sms:{sender}", model)
    print(f"[SMS] {sender}: {body!r}  ->  {reply[:120]!r}", flush=True)
    twiml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{escape(reply)}</Message></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


# --- iMessage via Sendblue --------------------------------------------------

def _send_imessage(number: str, content: str) -> None:
    """Send a blue-bubble iMessage through Sendblue's relay (their Macs)."""
    key_id = os.environ["SENDBLUE_API_KEY_ID"]
    secret = os.environ["SENDBLUE_API_SECRET"]
    req = urllib.request.Request(
        "https://api.sendblue.co/api/send-message",
        data=json.dumps({"number": number, "content": content}).encode(),
        method="POST",
    )
    req.add_header("sb-api-key-id", key_id)
    req.add_header("sb-api-secret-key", secret)
    req.add_header("content-type", "application/json")
    urllib.request.urlopen(req, timeout=15).read()


@messaging_router.post("/imessage")
async def imessage(request: Request, model: Model = Depends(get_model)):
    """Sendblue posts inbound iMessages here (JSON). We reply via their send API."""
    data = await request.json()
    # Sendblue echoes our own sent messages back as outbound — ignore those.
    if data.get("is_outbound") or str(data.get("status", "")).upper() in {"SENT", "DELIVERED"}:
        return {"ok": True, "skipped": "outbound"}
    text = (data.get("content") or "").strip()
    sender = data.get("number") or "imsg-unknown"
    if not text:
        return {"ok": True, "skipped": "empty"}
    reply = await crisis_reply(text, f"imsg:{sender}", model)
    print(f"[iMessage] {sender}: {text!r}  ->  {reply[:120]!r}", flush=True)
    try:
        _send_imessage(sender, reply)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}
