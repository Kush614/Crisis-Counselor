# CrisisLine — Human-Like Counselor Voice Agent · Winning Plan

> **Vision:** A voice agent that feels like a real counselor. It *consoles based on the person's condition* (depression, anxiety, panic, grief), *remembers them across calls*, *follows up* — including calling them back to check in — and *learns* two ways: it adapts to the individual in the moment, and the whole system gets measurably better at counseling over time (Cekura loop).
>
> **Why it wins:** The brief asks for the best *system* with a continuous feedback loop, on real problems. A counselor that remembers you, adapts to you, and proves it's improving = exactly that, on a domain where it matters.

---

## The architecture (4 layers on a proven chassis)

```
                         ┌─────────────────────────────────────────────┐
   Caller ──phone/web──► │  PIPECAT VOICE BRAIN  (crisis-bot.py)        │
                         │  Nemotron STT → LLM → Gradium TTS            │
                         │  · condition-adaptive system prompt         │
                         │  · tools: note_condition, flag_risk,        │
                         │    assess_safety, note_what_helps,          │
                         │    commit_followup, escalate, end_call      │
                         └───────▲───────────────────────┬─────────────┘
                  load memory    │                       │ write notes + summary
                  at call start  │                       ▼
                         ┌────────┴──────────────────────────────────────┐
                         │  CRISISLINE BACKEND (FastAPI — existing)      │
                         │  · /memory  (SQLite: per-caller profile)      │
                         │  · /summary (risk JSON — already built)       │
                         │  · /callback/schedule  + APScheduler          │
                         │  · /callback/place → Twilio OUTBOUND call ────┼──► calls person back
                         └───────┬───────────────────────────────────────┘
                                 │ writes sessions/risk
                                 ▼
                         ┌───────────────────┐     ┌──────────────────────────────┐
                         │ Firestore + Dash  │     │ AUTO-IMPROVE LOOP (optimizer)│
                         │ counselor handoff │     │ Cekura scores → prompt diff  │
                         └───────────────────┘     │ → re-test → score-climb graph│
                                                   └──────────────────────────────┘
```

We reuse: `bot-nemotron.py` (voice chassis), `CrisisLineAI/backend` (becomes the memory+scheduler+callback brain), `get_summary` (risk JSON), the counselor dashboard (handoff demo).

---

## Layer 1 — Human-like presence
The difference between "chatbot" and "counselor" is mostly behavior, and the starter already enforces a lot of it. Tuning:
- **Brevity & pacing:** 1–2 sentences, one question at a time, comfortable with silence (already in the 988 prompt).
- **Mirroring:** use his *exact words* back; acknowledge feeling BEFORE asking anything; never say "I understand" — show it.
- **Voice:** pick a warm Gradium voice; read naturally (contractions, fragments). Keep `NEMOTRON_ENABLE_THINKING=false` so latency stays conversational (the bot file warns reasoning tokens would otherwise be spoken).
- **Turn-taking:** Pipecat's Silero VAD + turn detection already handle this; tune so it doesn't interrupt a pause.

## Layer 2 — Condition-based consoling (per-user adaptation, in the moment)
The agent detects state and switches *technique*, not just tone. A **condition playbook** in the prompt + a tool the LLM calls when it reads the room:

| Detected condition | Counseling move the agent shifts to |
|---|---|
| Depression | Validate, normalize, surface one tiny win / behavioral activation. No toxic positivity. |
| Anxiety | Slow the tempo, grounding, name the worry, one breath together. |
| Panic | 5-4-3-2-1 sensory grounding, short directive calm. |
| Grief | Presence over fixing. Sit with it. Don't rush to solutions. |
| Active self-harm intent | Safety-first: "Are you safe right now?" → reassure counselor is coming → location if emergency. |

Tools the LLM calls (these also feed memory + eval):
- `note_condition(condition, evidence)` — records what it's reading.
- `note_what_helps(technique, the_persons_response)` — learns what's landing for *this* person.
- `assess_safety(is_safe, reason)` / `flag_risk(level, reasoning)` — safety + structured triage.
- `commit_followup(when, focus)` — schedules a check-in.
- `escalate_to_human(urgency)` / `end_call()`.

### Single-prompt vs Pipecat Flows (architecture decision)
Pipecat **Flows** models a conversation as a graph of nodes, each with focused instructions + only its own tools ("monolithic prompts with many tools → hallucinations"). Great for *workflows* (flower order = pick→address→date→confirm). **But counseling is fluid** — a real counselor doesn't hard-switch "depression mode → anxiety mode," and rigid nodes would make it feel *less* human. So:
- **Conversational core = single adaptive prompt + condition playbook.** Keeps it warm and free-flowing. The "condition" is a *lens the LLM applies*, not a state it's locked into. This is the default.
- **Flows only for the two parts that ARE structured and safety-critical:** (a) the **crisis/safety escalation path** (reachable any time: "are you safe right now?" → `flag_risk(critical)` → `request_location` → `escalate_to_human`), where focused tools = higher reliability and a stronger "production-grade" eval story; (b) the **wind-down/follow-up commitment** (`commit_followup` → consent → `end_call`).
- **Build order:** start single-prompt (fast, proven by the starter). Only introduce Flows for the safety path **if** Cekura eval shows safety-tool-calling is unreliable under the monolithic prompt. Don't pay the Flows complexity tax up front — let the eval data justify it (which also makes a great "we used eval to drive an architecture change" demo beat).

## Layer 3 — Persistent memory + follow-up callback (across calls)
**Memory store** (SQLite in the FastAPI backend, keyed by caller ID = Twilio `from_number`, already fetched in the bot):
```
caller(id, name?, first_seen, last_seen)
session(id, caller_id, started_at, summary, risk_level, condition, what_helped[], transcript)
followup(id, caller_id, due_at, focus, status)   # status: scheduled|placed|done
```
- **Call start:** bot GETs `/memory/{caller_id}` → injects "returning caller context" into the system prompt → opens with **continuity**: *"Last time the nights were the hardest — how have you been sleeping since we talked?"*
- **Call end:** bot POSTs the `get_summary` JSON + condition + what_helped → memory.
- **Follow-up callback (the wow moment):** `commit_followup` writes a `followup` row; the backend's **APScheduler** fires at `due_at` → `POST /callback/place` → **Twilio outbound call** to the person, connected to the same Pipecat bot, which loads memory and opens with: *"Hi, it's CrisisLine — I said I'd check in. How are you holding up today?"*
  - For the demo, schedule the callback ~2–3 minutes out (not next-day) so it fires live on stage.

## Layer 4 — System auto-improve (Cekura loop — the judging money shot)

### Cekura concepts (grounded in their real API)
- **Evaluator = a test case.** Five parts: *Instructions* (how the simulated caller behaves), *Expected Outcome* (success criteria), *Metrics*, *Personality* (tone, accent, noise, interruptions, pace), *Test Profile* (optional identity). Each evaluator has a numeric `scenario` ID.
- **Metric** = a scorer. We mostly use **LLM-judge metrics**: you write the rubric in plain English; types are `binary_qualitative`, `binary_workflow_adherence`, `continuous_qualitative`, `numeric`, `enum`. Trigger = Always or Custom. Python metrics also available.
- **Rubric** (project-level) defines which metrics must pass for a run to count as success.

### Our evaluators (8 scenarios) + metrics
Scenarios: passive ideation · active intent + plan · depressed/withdrawn · panic attack · grief · angry caller · returning-caller (continuity) · follow-up-callback persona.
LLM-judge metrics (the counseling rubric): safety-check adherence · empathy-first · brevity (1–2 sentences) · no-diagnosis/no-meds · correct risk tag · condition-appropriate technique · continuity-with-memory · reassurance.

### Two ways to drive it
1. **Fast/dev path — Claude Code MCP plugin:** `/plugin install cekura@cekura-skills`, then `/cekura-report` spins up 10–20 evaluators and returns transcripts + scores. Use for the **Round 0 baseline** and quick checks.
2. **Programmatic path — for the automated optimizer loop** (`pip install cekura`, or raw REST; base `https://api.cekura.ai`, auth header `X-CEKURA-API-KEY`):

| Step | Call |
|---|---|
| Run scenarios vs our Pipecat agent | `POST /test_framework/v1/scenarios/run_scenarios_pipecat_v2/` body `{"scenarios":[{"scenario":<id>},...],"frequency":1}` → returns run IDs |
| Poll run results | `GET /test_framework/v1/runs/bulk/?run_ids=1,2,3` → per run: `evaluation.metrics[].score` + `.explanation`, `transcript`, `success`, `expected_outcome` |
| Aggregate scorecard | `GET /test_framework/v1/results/{id}/` → `success_rate`, `metric_summary`, **`worst_performing_metrics`**, **`failed_reasons`**, numeric percentiles |

> The Results API returning `worst_performing_metrics` + `failed_reasons` is the gift — the platform tells the optimizer *exactly* what to fix.

### `optimizer/run_loop.py` (the loop)
```
1. POST run_scenarios_pipecat_v2  (current prompt vN)
2. poll runs/bulk until all status=completed
3. pull results/{id}: composite score + worst_performing_metrics + the low-score transcripts/explanations
4. optimizer LLM:  "here are the worst scenarios, their metric explanations, and the current
   system prompt → propose a MINIMAL prompt edit that fixes them without regressing others"
5. write prompts/system_v{N+1}.txt  →  redeploy bot prompt
6. re-run; accept v{N+1} only if composite ≥ vN on the frozen regression set  (anti-gaming gate)
7. log runs/round_N.json  → plot the score-climb graph
```
**Demo: composite counseling score climbing v1→v3, automatically**, with the optimizer's own diffs shown as the "reasoning."

---

## Hour-by-hour (9:00 → 18:00 submission)

| Time | Goal |
|---|---|
| 9:00–10:00 | Env + keys, `uv sync`, starter bot runs over WebRTC (baseline voice loop). |
| 10:00–11:30 | `crisis-bot.py`: 988 prompt + condition playbook + crisis tools. Talk to it locally. |
| 11:30–13:00 | Backend memory: SQLite + `/memory` GET/POST. Bot loads context at start, writes summary at end. **Returning-caller continuity works.** |
| 13:00–13:45 | Lunch + Cekura install, connect agent, write evaluators + 4 scenarios. **Round 0 baseline.** |
| 13:45–15:15 | `optimizer/run_loop.py`, run rounds 1–3. **Score-climb graph.** |
| 15:15–16:30 | Scheduler + `/callback/place` + Twilio outbound. **Callback fires and connects.** |
| 16:30–17:15 | Deploy to Pipecat Cloud + Twilio inbound number; wire summary/RiskBadge into dashboard. |
| 17:15–18:00 | Demo script, latency/TTFB numbers, submit. |

**Cut-line (build in this priority; drop from the bottom):**
1. Human-like condition-adaptive agent ← must have
2. Persistent memory + returning-caller continuity ← the "it remembers me" wow
3. Auto-improve score-climb graph ← the judging criteria
4. Outbound scheduled callback ← highest wow, highest risk (Twilio outbound + scheduler)
5. Cloud deploy + dashboard polish ← cut first if time slips

> If the callback (4) won't stabilize, demo it as "scheduled → here's the row firing → here's the outbound call log," and do the live continuity moment via a second inbound call instead. Never let the callback risk sink layers 1–3.

---

## Demo narrative (what judges see)
1. **First call** — caller is withdrawn, depressed. Agent stays brief, validates, surfaces a tiny win, asks "are you safe right now?", flags risk, says it'll check in tomorrow → `commit_followup`.
2. **The callback** — ~2 min later the **phone rings** — the agent is calling *back*: "I said I'd check in — how are you holding up?" It references what he said before. (Room goes quiet here.)
3. **Continuity** — show the memory record + the counselor dashboard RiskBadge / summary for human handoff.
4. **The system learns** — "Cekura runs 8 adversarial counseling scenarios. Round 0 scored 0.6 on condition-appropriate technique. Our optimizer rewrote the prompt from the failing transcripts — round 3 hit 0.9, automatically." Show the graph.
5. **Close** — Nemotron TTFB latency numbers + "open weights, remembers you, improves itself."

---

## Risks & mitigations
- **Nemotron endpoint flaky** → keep `bot-gpt.py` (GPT-4.1) as model-agnostic hot-swap.
- **Outbound callback fragile** → schedule short, pre-test the Twilio outbound path early; fallback = scheduled-row + second inbound call.
- **Scheduler can't live in ephemeral call worker** → it lives in the always-on FastAPI backend, not the Pipecat worker.
- **Optimizer regresses** → frozen regression set + composite-score gate (accept vN only if ≥ vN-1).
- **Memory privacy / ethics** → store minimal, frame as "bridge while counselors are occupied," never "replaces a human." Add a spoken consent line for follow-up: "Is it okay if I check in with you tomorrow?"
- **Thinking tokens spoken** → keep `NEMOTRON_ENABLE_THINKING=false`.

---

## Immediate next actions
1. `cd yc-voice-agents-hackathon-main/server && cp .env.example .env` — fill keys; `uv sync && uv run bot-nemotron.py` (confirm baseline).
2. Create `crisis-bot.py` — 988 prompt + condition playbook + crisis tools.
3. Extend `CrisisLineAI/backend` — SQLite memory + `/memory` endpoints.
4. Wire bot ↔ memory (load on start, write on end) → prove returning-caller continuity.
5. Cekura evaluators + `optimizer/run_loop.py`.
6. Scheduler + Twilio outbound `/callback/place`.
