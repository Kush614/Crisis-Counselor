#
# CrisisLine — counselor voice agent
# Forked from bot-nemotron.py (YC Voice Agents Hackathon starter, (c) Daily, BSD-2).
#

"""CrisisLine — a human-like crisis support voice agent for the 988 Lifeline.

It steps in while human counselors are occupied: stays present, adapts its
counseling technique to the caller's condition (depression / anxiety / panic /
grief / anger / numbness), remembers people across calls, and can promise a
follow-up check-in.

Pipeline: Nemotron Speech Streaming STT -> Nemotron-3-Super-120B LLM -> Gradium TTS,
with crisis-support tools registered on the LLM context.

Run the bot using::

    uv run crisis-bot.py
"""

import os
from datetime import date

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from crisis_memory import MemoryClient, build_caller_context, build_followup_context
from crisis_prompt import CONDITION_PLAYBOOK, build_system_instruction

load_dotenv(override=True)

# Caller id used when there is no phone number (WebRTC local dev). A fixed value
# means cross-call memory/continuity is testable in the browser.
LOCAL_CALLER_ID = os.getenv("CRISIS_LOCAL_CALLER_ID", "webrtc-local")


async def get_call_info(call_sid: str) -> dict:
    """Fetch caller info (from/to numbers) from the Twilio REST API."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    try:
        auth = aiohttp.BasicAuth(account_sid, auth_token)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    logger.error(f"Twilio API error ({response.status}): {await response.text()}")
                    return {}
                data = await response.json()
                return {"from_number": data.get("from"), "to_number": data.get("to")}
    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return {}


async def run_bot(
    transport: BaseTransport,
    caller_id: str | None = None,
    is_followup: bool = False,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        caller_id: The human's identifier (their phone number, or LOCAL_CALLER_ID
            for WebRTC) — the key for cross-call memory.
        is_followup: True when WE initiated this call as a promised check-in
            (i.e. an outbound call), so the bot opens with the follow-up greeting.
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
    """
    logger.info("Starting CrisisLine bot")

    caller_id = caller_id or LOCAL_CALLER_ID
    # Env override lets us exercise the follow-up opener locally over WebRTC.
    is_followup = is_followup or os.getenv("CRISIS_IS_FOLLOWUP", "false").lower() == "true"

    # --- Memory: load this caller's history before building the prompt -------
    memory = MemoryClient()
    record = await memory.load_caller(caller_id)
    caller_context = build_caller_context(record)
    followup_context = build_followup_context(record) if is_followup else ""
    returning = bool(caller_context)
    logger.info(f"Caller {caller_id}: returning={returning} followup={bool(followup_context)}")

    # Per-call structured notes. Closed over by the tools; persisted at call end.
    session: dict = {
        "caller_id": caller_id,
        "condition": None,
        "condition_history": [],
        "risk_level": None,
        "risk_events": [],
        "safety_checks": [],
        "what_helped": [],
        "followup": None,
        "escalation": None,
        "summary": None,
    }

    # --- Crisis-support tools the LLM can call -------------------------------

    async def note_condition(params: FunctionCallParams, condition: str, evidence: str) -> None:
        """Record the emotional condition you're reading in the caller, so you stay
        anchored in the right counseling technique. Call this whenever your read of
        their state forms or changes.

        Args:
            condition: One of: depression, anxiety, panic, grief, anger, numb, other.
            evidence: The words or signals that led you to this read.
        """
        cond = condition.strip().lower()
        session["condition"] = cond
        session["condition_history"].append({"condition": cond, "evidence": evidence})
        technique = CONDITION_PLAYBOOK.get(cond, "Stay present, validate, and match their pace.")
        logger.info(f"note_condition: {cond} — {evidence}")
        memory.emit_event(caller_id, "condition", cond, evidence)
        await params.result_callback({"ok": True, "technique": technique})

    async def assess_safety(params: FunctionCallParams, is_safe: bool, concern: str = "") -> None:
        """Record the result of a safety check after you've gently asked whether
        they are safe right now. Call this any time safety is in question.

        Args:
            is_safe: True if they confirmed they're safe right now, else False.
            concern: What they said about their safety, in brief.
        """
        session["safety_checks"].append({"is_safe": is_safe, "concern": concern})
        logger.info(f"assess_safety: is_safe={is_safe} — {concern}")
        memory.emit_event(caller_id, "safety", "safe" if is_safe else "unsafe", concern)
        guidance = (
            "Keep gently staying with them and checking in."
            if is_safe
            else "They are NOT confirmed safe — prioritize finding out where they are and call "
            "escalate_to_human to bring in a live counselor."
        )
        await params.result_callback({"ok": True, "guidance": guidance})

    async def flag_risk(params: FunctionCallParams, level: str, reasoning: str) -> None:
        """Record your current read of the caller's risk level. Call whenever it changes.

        Args:
            level: One of: low, medium, high, critical.
            reasoning: Why you assigned this level.
        """
        lvl = level.strip().lower()
        session["risk_level"] = lvl
        session["risk_events"].append({"level": lvl, "reasoning": reasoning})
        logger.info(f"flag_risk: {lvl} — {reasoning}")
        memory.emit_event(caller_id, "risk", lvl, reasoning)
        await params.result_callback({"ok": True, "recorded_level": lvl})

    async def note_what_helps(params: FunctionCallParams, technique: str, response: str) -> None:
        """Record that something you tried clearly landed for THIS person, so it can
        be remembered for next time.

        Args:
            technique: What you did (e.g. "5-4-3-2-1 grounding", "naming the worry").
            response: How they responded that told you it helped.
        """
        session["what_helped"].append({"technique": technique, "response": response})
        logger.info(f"note_what_helps: {technique}")
        memory.emit_event(caller_id, "helped", technique, response)
        await params.result_callback({"ok": True})

    async def commit_followup(params: FunctionCallParams, when: str, focus: str) -> None:
        """Promise a follow-up check-in. Only call AFTER they've agreed to be checked
        in on. This schedules a real callback.

        Args:
            when: When to check in, in the caller's own words (e.g. "tomorrow", "tonight").
            focus: What to follow up about (e.g. "how the night went").
        """
        session["followup"] = {"when": when, "focus": focus}
        await memory.schedule_followup(caller_id, when=when, focus=focus)
        logger.info(f"commit_followup: {when} — {focus}")
        memory.emit_event(caller_id, "followup", when, focus)
        await params.result_callback({"ok": True, "when": when})

    async def escalate_to_human(
        params: FunctionCallParams, urgency: str, reason: str = ""
    ) -> None:
        """Flag a live counselor to join. Use when safety is at risk or the situation
        is beyond a supportive holding conversation.

        Args:
            urgency: One of: routine, soon, immediate.
            reason: Why a human is needed now.
        """
        session["escalation"] = {"urgency": urgency, "reason": reason}
        logger.info(f"ESCALATION urgency={urgency} reason={reason}")
        memory.emit_event(caller_id, "escalation", urgency, reason)
        await params.result_callback(
            {"ok": True, "message": "A live counselor has been flagged to join."}
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call this AFTER you have said a warm goodbye in the same
        turn. The pipeline flushes any queued speech and then hangs up."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        note_condition,
        assess_safety,
        flag_risk,
        note_what_helps,
        commit_followup,
        escalate_to_human,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction (persona + playbook + memory continuity) ---------
    system_instruction = build_system_instruction(
        caller_context=caller_context,
        followup_context=followup_context,
    )
    system_instruction += (
        f"\n\nToday is {date.today().strftime('%A, %B %d, %Y')}. Use this for relative "
        'times like "tomorrow" or "tonight" when scheduling a follow-up.'
    )

    # --- STT + LLM services (backend-switchable) -----------------------------
    # Default is OpenAI (Gradium STT + GPT-4.1) so the bot runs with just Gradium
    # + OpenAI keys — no NVIDIA needed. At the event, set CRISIS_LLM_BACKEND=nemotron
    # to use the NVIDIA Nemotron STT + LLM endpoints instead.
    backend = os.getenv("CRISIS_LLM_BACKEND", "openai").lower()
    logger.info(f"LLM backend: {backend}")

    if backend == "nemotron":
        from nemotron_llm import VLLMOpenAILLMService
        from nvidia_stt import NVidiaWebSocketSTTService

        stt = NVidiaWebSocketSTTService(
            url=os.getenv("NVIDIA_ASR_URL", "ws://44.241.251.184:8080"),
            strip_interim_prefix=True,
        )
        # Keep thinking OFF for voice: with no reasoning parser on the server,
        # chain-of-thought would be spoken aloud.
        enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
        llm = VLLMOpenAILLMService(
            api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
            base_url=os.getenv(
                "NEMOTRON_LLM_URL",
                "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1",
            ),
            settings=VLLMOpenAILLMService.Settings(
                model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
                system_instruction=system_instruction,
                extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
            ),
        )
    else:  # openai (default) — Gradium STT + OpenAI GPT-4.1 Responses API
        from pipecat.services.gradium.stt import GradiumSTTService
        from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService
        from pipecat.transcriptions.language import Language

        stt = GradiumSTTService(
            api_key=os.environ["GRADIUM_API_KEY"],
            settings=GradiumSTTService.Settings(language=Language.EN),
        )
        llm = OpenAIResponsesLLMService(
            api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAIResponsesLLMService.Settings(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
                system_instruction=system_instruction,
            ),
        )

    # Text-to-Speech — Gradium. Pick a warm voice for a counselor.
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_"),
        ),
    )

    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Shorter end-of-turn silence so the bot replies sooner after you stop
            # (default ~0.8s felt laggy on the phone). 0.45s stays gentle enough
            # not to cut off a caller mid-pause.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.45, start_secs=0.2)),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    async def persist_session() -> None:
        """Write this call's notes + transcript to memory. Best-effort."""
        try:
            session["transcript"] = context.get_messages()
        except Exception as e:
            logger.debug(f"Could not capture transcript: {e}")
        await memory.save_session(caller_id, session)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Caller connected")
        memory.emit_event(
            caller_id, "call",
            "followup" if followup_context else ("returning" if returning else "new"),
            caller_id,
        )
        # Choose the opener: follow-up call > returning caller > brand new.
        if followup_context:
            opener = (
                "You are calling them back to check in, as you promised. Greet them gently and "
                "reference that you said you'd follow up. Keep it warm and brief."
            )
        elif returning:
            opener = (
                "This caller has talked with you before (see CALLER CONTEXT). Greet them with warm "
                "continuity, reference what they were going through last time, and ask how they've "
                "been since. Keep it to one or two sentences."
            )
        else:
            opener = (
                "Greet them warmly and simply: 'You've reached 988. I'm here with you — what's "
                "going on tonight?' Let them lead."
            )
        context.add_message({"role": "user", "content": f"[The caller just connected.] {opener}"})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Caller disconnected")
        await persist_session()
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    caller_id: str | None = None
    is_followup = False
    transport_overrides: dict = {}
    twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "")

    # Krisp noise filter is available when deployed to Pipecat Cloud.
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case WebSocketRunnerArguments():
            # Twilio media streams are 8 kHz μ-law both directions.
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            _, call_data = await parse_telephony_websocket(runner_args.websocket)
            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                frm = call_info.get("from_number")
                to = call_info.get("to_number")
                logger.info(f"Call from: {frm} to: {to}")
                # If WE are the 'from' (our Twilio number), this is an outbound
                # follow-up we placed — the human is the 'to'. Otherwise it's an
                # inbound call and the human is the 'from'.
                if frm and twilio_number and frm == twilio_number:
                    caller_id, is_followup = to, True
                    logger.info(f"Outbound follow-up call to {to}")
                else:
                    caller_id = frm

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )
            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, caller_id=caller_id, is_followup=is_followup, **transport_overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
