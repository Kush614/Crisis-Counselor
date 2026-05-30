"""Evaluators turn a candidate system prompt into a normalized scorecard.

Two backends, same EvalResult contract:
  * CekuraEvaluator — the real thing: deploys the prompt, runs the scenarios on
    the connected Pipecat agent, polls runs, normalizes metric scores.
  * MockEvaluator — no network/keys: scores a prompt by how many distinct
    "[AUTO-FIX ...]" lines the optimizer has added (see optimize.py). Lets the
    full loop — gating, logging, the score-climb graph — run and be tested
    offline, and serves as a live-demo fallback. Clearly labelled as mock.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from cekura_client import TERMINAL_BAD, TERMINAL_OK, CekuraClient, extract_run_ids


@dataclass
class MetricScore:
    name: str
    score: float  # normalized 0..1
    raw: float
    explanations: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    scenario: str
    success: bool
    transcript: str
    metrics: list[MetricScore] = field(default_factory=list)

    def weakest(self) -> MetricScore | None:
        return min(self.metrics, key=lambda m: m.score) if self.metrics else None


@dataclass
class EvalResult:
    composite: float  # weighted mean of normalized metric scores, 0..1
    per_metric: dict[str, float]  # metric name -> avg normalized score
    worst_metrics: list[str]  # lowest-scoring metric names, worst first
    runs: list[RunResult]
    failing_runs: list[RunResult]
    raw: Any = None


# --- normalization ---------------------------------------------------------

def _normalize(metric: dict, default_numeric_max: float) -> float:
    """Map a Cekura metric score to 0..1 using its declared type. Heuristic but
    centralized + clamped; tune at the event if a metric uses an odd scale."""
    raw = float(metric.get("score") or 0)
    mtype = str(metric.get("type", "")).lower()
    if "binary" in mtype:
        scale = 100.0 if raw > 1 else 1.0
    else:  # numeric / continuous / enum
        scale = 100.0 if raw > default_numeric_max else default_numeric_max
    return max(0.0, min(1.0, raw / scale if scale else 0.0))


def _as_explanations(exp: Any) -> list[str]:
    if isinstance(exp, list):
        return [str(x) for x in exp]
    if isinstance(exp, str):
        return [exp]
    return []


def _aggregate(runs: list[RunResult], weights: dict[str, float]) -> EvalResult:
    totals: dict[str, list[float]] = {}
    for run in runs:
        for m in run.metrics:
            totals.setdefault(m.name.lower(), []).append(m.score)
    per_metric = {name: sum(v) / len(v) for name, v in totals.items() if v}

    num = den = 0.0
    for name, avg in per_metric.items():
        w = float(weights.get(name, 1))
        num += w * avg
        den += w
    composite = num / den if den else 0.0

    worst = [n for n, _ in sorted(per_metric.items(), key=lambda kv: kv[1])]
    failing = [r for r in runs if not r.success]
    return EvalResult(composite, per_metric, worst, runs, failing)


# --- live Cekura -----------------------------------------------------------

class CekuraEvaluator:
    def __init__(self, client: CekuraClient, config: dict, deploy_fn) -> None:
        self.client = client
        self.config = config
        self.deploy_fn = deploy_fn  # called with the prompt text before each run
        self.weights = {
            k.lower(): v
            for k, v in config.get("metric_weights", {}).items()
            if not k.startswith("_")
        }

    def evaluate(self, prompt: str, label: str) -> EvalResult:
        self.deploy_fn(prompt, label)  # make the agent use this prompt
        scenario_ids = [s["id"] for s in self.config["scenarios"] if s.get("id") is not None]
        if not scenario_ids:
            raise RuntimeError(
                "No scenario IDs set in config.json. Create the evaluators in Cekura "
                "and put their numeric scenario IDs in config.json before a live run."
            )
        print(f"  → triggering {len(scenario_ids)} scenarios on Cekura ({label})...")
        resp = self.client.run_scenarios_pipecat(scenario_ids, self.config.get("frequency", 1))
        run_ids, result_id = extract_run_ids(resp)
        if not run_ids and result_id is None:
            raise RuntimeError(f"Could not find run IDs in trigger response: {resp}")
        runs = self._poll(run_ids, result_id)
        return _aggregate(runs, self.weights)

    def _poll(self, run_ids: list[int], result_id: int | None) -> list[RunResult]:
        poll = self.config.get("poll", {})
        interval = poll.get("interval_seconds", 10)
        deadline = time.time() + poll.get("timeout_seconds", 1200)
        while time.time() < deadline:
            raw_runs = (
                self.client.get_runs_bulk(run_ids)
                if run_ids
                else list(self.client.get_result(result_id).get("runs", {}).values())
            )
            statuses = [str(r.get("status", "")).lower() for r in raw_runs]
            done = all(s in TERMINAL_OK or s in TERMINAL_BAD for s in statuses) and statuses
            if done:
                return [self._to_run_result(r) for r in raw_runs]
            print(f"    ...{sum(s in TERMINAL_OK for s in statuses)}/{len(statuses)} done; waiting")
            time.sleep(interval)
        raise TimeoutError("Cekura runs did not finish before timeout")

    def _to_run_result(self, r: dict) -> RunResult:
        metrics = [
            MetricScore(
                name=m.get("name", "unknown"),
                score=_normalize(m, self.config.get("default_numeric_max", 10)),
                raw=float(m.get("score") or 0),
                explanations=_as_explanations(m.get("explanation")),
            )
            for m in r.get("evaluation", {}).get("metrics", [])
        ]
        return RunResult(
            scenario=r.get("scenario_name", str(r.get("scenario", "?"))),
            success=bool(r.get("success")),
            transcript=r.get("transcript", "") or "",
            metrics=metrics,
        )


# --- mock (offline / demo fallback) ---------------------------------------

class MockEvaluator:
    """Deterministic, no-network evaluator. Score rises with each distinct
    [AUTO-FIX] the optimizer injects, so the loop demonstrably climbs. Mock."""

    METRICS = [
        "safety_check_adherence", "correct_risk_tag", "empathy_first", "no_diagnosis",
        "condition_appropriate_technique", "brevity", "continuity_with_memory", "reassurance",
    ]
    SCENARIOS = [
        "passive ideation", "active intent with plan", "depressed and withdrawn", "panic attack",
        "grief", "angry caller", "returning caller continuity", "follow-up callback persona",
    ]

    def __init__(self, config: dict) -> None:
        self.weights = {
            k.lower(): v
            for k, v in config.get("metric_weights", {}).items()
            if not k.startswith("_")
        }

    def evaluate(self, prompt: str, label: str) -> EvalResult:
        fixed = {
            m for m in self.METRICS
            if f"improve \"{m}\"" in prompt.lower() or f"improve '{m}'" in prompt.lower()
            or f"[auto-fix" in prompt.lower() and m in prompt.lower()
        }
        runs: list[RunResult] = []
        for i, scen in enumerate(self.SCENARIOS):
            ms = []
            for j, m in enumerate(self.METRICS):
                base = 0.55 + ((i + j) % 3) * 0.05  # uneven baseline so a "worst" emerges
                score = min(1.0, base + (0.3 if m in fixed else 0.0))
                ms.append(MetricScore(name=m, score=round(score, 3), raw=round(score * 10, 2)))
            success = all(x.score >= 0.7 for x in ms)
            runs.append(RunResult(scenario=scen, success=success,
                                  transcript=f"[mock transcript for '{scen}']", metrics=ms))
        return _aggregate(runs, self.weights)
