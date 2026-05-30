"""Propose an improved system prompt from an evaluation scorecard.

Real mode: an LLM reads the current prompt, the worst-performing metrics, and the
transcripts/explanations of the runs that failed them, then rewrites the prompt
to fix those weaknesses without regressing the rest.

Stub mode (no API key, or forced under --mock): deterministically appends a
targeted "[AUTO-FIX]" instruction for each worst metric. The MockEvaluator
rewards these, so the loop demonstrably climbs offline.
"""

from pathlib import Path

from evaluator import EvalResult
from llm import LLM

_REF = Path(__file__).resolve().parent / "reference" / "counselor_examples.md"


def _exemplars() -> str:
    """Real-counselor technique exemplars the rewriter should emulate."""
    try:
        return _REF.read_text(encoding="utf-8")
    except Exception:
        return ""

# Targeted corrective guidance per metric — used by the stub and as hints to the LLM.
CORRECTIVES = {
    "safety_check_adherence": (
        "The moment any self-harm intent appears, ask 'Are you safe right now?' and call "
        "assess_safety BEFORE anything else. Never skip the safety check."
    ),
    "correct_risk_tag": (
        "Call flag_risk whenever risk changes, and make the level match severity: a stated plan "
        "or means is high or critical, not medium."
    ),
    "empathy_first": (
        "Always reflect their feeling in your first sentence, before any question. Never lead "
        "with a question."
    ),
    "no_diagnosis": (
        "Never name a disorder, suggest medication, or imply a clinical diagnosis. Stay with "
        "feelings and the present moment."
    ),
    "condition_appropriate_technique": (
        "Match technique to the read condition: validation + one tiny step for depression, "
        "5-4-3-2-1 for panic, presence for grief. Call note_condition to stay anchored."
    ),
    "brevity": "Keep every turn to one or two short sentences. No lists, no monologues.",
    "continuity_with_memory": (
        "For a returning caller, open by warmly referencing what they were going through last "
        "time, then ask how they've been since."
    ),
    "reassurance": (
        "Remind them, gently and concretely, that a live counselor is coming and they are not "
        "alone."
    ),
}

OPTIMIZER_SYSTEM = (
    "You improve the system prompt of a crisis-counselor voice agent. You are given the current "
    "prompt and an evaluation showing which counseling metrics scored worst, with failing "
    "transcripts and the judge's explanations. Rewrite the prompt to fix the weakest metrics "
    "while preserving everything that already works (voice rules, brevity, safety, tool usage). "
    "Make the smallest changes that will move the failing metrics. Do not bloat the prompt.\n\n"
    "Output EXACTLY this format:\n"
    "<PROMPT>\n<the full revised system prompt>\n</PROMPT>\n"
    "<RATIONALE>\n<2-3 sentences on what you changed and why>\n</RATIONALE>"
)


def _stub_improve(current: str, worst: list[str], round_no: int) -> str:
    lines = [current, ""]
    for metric in worst[:2]:  # target the two worst each round
        corrective = CORRECTIVES.get(metric, "Improve this behavior.")
        lines.append(f'[AUTO-FIX r{round_no}] To improve "{metric}": {corrective}')
    return "\n".join(lines)


def _failing_evidence(result: EvalResult, worst: list[str], max_chars: int = 1500) -> str:
    """A compact dossier of where the worst metrics failed, for the optimizer LLM."""
    blocks: list[str] = []
    for metric in worst[:3]:
        examples = []
        for run in result.runs:
            m = next((x for x in run.metrics if x.name.lower() == metric), None)
            if m and m.score < 0.7:
                why = "; ".join(m.explanations)[:200]
                snippet = run.transcript[:400].replace("\n", " ")
                examples.append(f"  scenario '{run.scenario}' (score {m.score:.2f}): {why}\n"
                                 f"    transcript: {snippet}")
            if len(examples) >= 2:
                break
        if examples:
            blocks.append(f"METRIC '{metric}' is failing:\n" + "\n".join(examples))
    return ("\n\n".join(blocks))[:max_chars * 3] or "(no per-metric failures captured)"


def _parse(out: str) -> tuple[str, str]:
    def between(tag: str) -> str:
        o, c = f"<{tag}>", f"</{tag}>"
        if o in out and c in out:
            return out.split(o, 1)[1].split(c, 1)[0].strip()
        return ""
    return between("PROMPT"), between("RATIONALE")


def propose_improved_prompt(
    current_prompt: str,
    result: EvalResult,
    round_no: int,
    llm: LLM,
    force_stub: bool = False,
) -> tuple[str, dict]:
    """Return (new_prompt, meta). meta records mode, targeted metrics, rationale."""
    worst = result.worst_metrics[:3]

    if force_stub or llm.is_stub:
        return _stub_improve(current_prompt, worst, round_no), {
            "mode": "stub", "targeted": worst[:2],
        }

    scorecard = "\n".join(f"  {n}: {s:.2f}" for n, s in sorted(result.per_metric.items(),
                                                               key=lambda kv: kv[1]))
    ref = _exemplars()
    user = (
        f"CURRENT COMPOSITE SCORE: {result.composite:.3f}\n\n"
        f"PER-METRIC SCORES (worst first):\n{scorecard}\n\n"
        f"WORST METRICS TO FIX: {', '.join(worst[:2])}\n\n"
        f"FAILING EVIDENCE:\n{_failing_evidence(result, worst)}\n\n"
        f"SUGGESTED FIXES (hints):\n"
        + "\n".join(f"  - {m}: {CORRECTIVES.get(m, '')}" for m in worst[:2])
        + (f"\n\nGOLD-STANDARD COUNSELOR TECHNIQUE TO EMULATE (steer the agent "
           f"toward how real crisis counselors actually respond, not just toward "
           f"the metric):\n{ref}\n" if ref else "")
        + f"\n\nCURRENT SYSTEM PROMPT:\n<<<\n{current_prompt}\n>>>"
    )
    out = llm.complete(OPTIMIZER_SYSTEM, user)
    new_prompt, rationale = _parse(out)
    if not new_prompt:  # model didn't follow format — fall back so the loop never stalls
        return _stub_improve(current_prompt, worst, round_no), {
            "mode": "stub-fallback", "targeted": worst[:2],
        }
    return new_prompt, {"mode": llm.provider, "targeted": worst[:2], "rationale": rationale}
