"""Avatar reply — text → OpenAI TTS → Simli avatar → MP4, for the chat thread.

The chat calls POST /avatar_reply with the agent's reply text; we render a short
talking-avatar video (h264/aac) and stream it back. The frontend drops it into
the message bubble, autoplays it ("talks"), then removes it ("leaves").

Proven feasible: Simli's FileRenderer writes the MP4 directly (ffmpeg/PyAV).
Heavier than text (~one Simli session per reply, several seconds), so the chat
shows it as an opt-in flourish, not on every message.
"""

import asyncio
import os
import tempfile
import uuid

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import Response
from openai import OpenAI
from simli import SimliClient, SimliConfig
from simli.renderers.renderers import FileRenderer

avatar_router = APIRouter()
_oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY") or os.getenv("OPEN_AI_APIKEY"))


def _tts_16k_pcm(text: str) -> bytes:
    """OpenAI TTS (24kHz PCM16) -> 16kHz mono PCM16 that Simli expects."""
    pcm24 = _oai.audio.speech.create(
        model="tts-1", voice="shimmer", input=text, response_format="pcm"
    ).content
    s = np.frombuffer(pcm24, dtype=np.int16)
    n = int(len(s) * 16000 / 24000)
    idx = (np.arange(n) * 24000 / 16000).astype(int)
    return s[idx].astype("<i2").tobytes()


async def _render_mp4(text: str) -> bytes:
    pcm = await asyncio.get_event_loop().run_in_executor(None, _tts_16k_pcm, text)
    secs = len(pcm) / (16000 * 2)
    out = os.path.join(tempfile.gettempdir(), f"avatar_{uuid.uuid4().hex}.mp4")

    sc = SimliClient(
        os.environ["SIMLI_API_KEY"],
        config=SimliConfig(
            faceId=os.getenv("SIMLI_FACE_ID", "tmp9i8bbq7c"),
            maxIdleTime=5, maxSessionLength=60,
        ),
    )
    await sc.start()
    try:
        renderer = FileRenderer(sc, filename=out, audioCodec="aac")
        task = asyncio.create_task(renderer.render())
        await sc.sendSilence(0.2)
        await sc.send(pcm)
        await sc.sendSilence(0.4)
        await asyncio.sleep(secs + 2.5)   # let the avatar finish speaking
        await sc.stop()
        await task
    finally:
        try:
            await sc.stop()
        except Exception:
            pass

    with open(out, "rb") as f:
        data = f.read()
    try:
        os.remove(out)
    except Exception:
        pass
    return data


@avatar_router.post("/avatar_reply")
async def avatar_reply(req: Request):
    """Render the given reply text as a talking-avatar MP4."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    text = (body.get("text") or "").strip()[:400]
    if not text:
        return Response(status_code=400)
    try:
        mp4 = await _render_mp4(text)
    except Exception as e:
        return Response(content=f"avatar error: {e}", status_code=500)
    return Response(content=mp4, media_type="video/mp4",
                    headers={"Cache-Control": "no-store"})
