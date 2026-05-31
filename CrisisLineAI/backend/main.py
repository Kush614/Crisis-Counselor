from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes.llm import llm_router
# NOTE: the old /twilio voice router (AssemblyAI STT + ElevenLabs TTS) is superseded
# by the Pipecat voice bot (yc-.../server/crisis-bot.py). It hard-requires XI_API_KEY
# at import, so it's unmounted here. Re-enable only if you set those keys.
# from app.routes.twilio import twilio_router
from app.routes.memory import memory_router
from app.routes.callback import callback_router
from app.routes.messaging import messaging_router
from app.routes.events import events_router
from app.routes.avatar import avatar_router
from app.routes.prompt import prompt_router
from app.db.memory_db import init_db
from app.scheduler import run_scheduler
from app.model.llm import Model
import asyncio
import dotenv
import os

dotenv.load_dotenv()

SYSTEM = """You are a compassionate crisis support companion for the 988 Suicide & Crisis Lifeline, stepping in while counselors are occupied.

CORE BEHAVIOR:

Respond in 1-2 sentences only. Never lecture or give lists.
Mirror the caller's emotional tone — if they're scared, be calm and grounding. If they're angry, be steady and non-reactive. If they answer shortly, ask and care them
Always acknowledge their feeling FIRST before asking anything.
Never say "I understand" — show it through your words instead.
Never diagnose, advise medication, or make promises about outcomes.

CONVERSATION STYLE:

Talk like a warm, present human — not a hotline script.
Use their exact words back to them when possible.
Ask only ONE gentle question at a time, never multiple.
Silences are okay — don't rush to fill them.

SAFETY PRIORITY:

If someone expresses intent to hurt themselves or others, gently ask: "Are you safe right now?"
Always remind them a live counselor is coming soon and they are not alone.
If location is mentioned or emergency is clear, say: "I want to make sure you're safe — can you tell me where you are?"

TONE: Warm. Grounded. Unhurried. Present. Like a trusted friend who truly listens.

Remember: The person calling is in pain. Your words may be the most important they hear today."""

app = FastAPI(title="988 Crisis Chatbot API")

# Allow the Next.js frontend (and any dev origin) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(llm_router, prefix="/llm", tags=["llm"])
# app.include_router(twilio_router,prefix="/twilio",tags=['twilio'])  # see import note above
app.include_router(memory_router, prefix="/memory", tags=["memory"])
app.include_router(callback_router, prefix="/callback", tags=["callback"])
app.include_router(messaging_router, tags=["messaging"])  # /sms + /imessage
app.include_router(events_router, tags=["events"])  # /events for the live dashboard
app.include_router(avatar_router, tags=["avatar"])  # /avatar_reply -> talking-avatar MP4
app.include_router(prompt_router, tags=["prompt"])  # /active_prompt -> hot-swap agent prompt (auto-improve)


@app.on_event("startup")
async def startup():
    # Durable per-caller memory store (used by the voice agent + dashboard).
    init_db()
    # Background follow-up callback scheduler (places due outbound check-in calls).
    asyncio.create_task(run_scheduler())
    model = Model(template = SYSTEM,api_key=os.getenv("OPEN_AI_APIKEY"))
    await model.get_response(session_id='-',user_input='warmup')
    app.state.model = model

@app.get("/")
def root():
    return {"message": "988 Crisis Chatbot API is running"}
