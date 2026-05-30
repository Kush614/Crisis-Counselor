"""Auto-improve loop: evaluate the counselor prompt, rewrite it from the failing
metrics, re-evaluate, keep it only if the composite score improved. Repeat.

Usage:
    python run_loop.py --mock                # offline, no keys — tests + demo fallback
    python run_loop.py --rounds 3            # live: needs CEKURA_API_KEY + scenario IDs in config
    python run_loop.py --mock --rounds 4

Outputs (in optimizer/runs/):
    round_<n>.json   per-round scorecard + optimizer meta
    scores.json      the score-climb series (plot this for the demo)
    summary.txt      human-readable recap
The live best prompt is written to the configured prompt_file (the seam the bot
reads via CRISIS_PROMPT_FILE).
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cekura_client import CekuraClient
from evaluator import CekuraEvaluator, EvalResult, MockEvaluator
from llm import LLM
from optimize import propose_improved_prompt

# Windows consoles default to cp1252 and choke on the bar/check glyphs below.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent


@dataclass
class Round:
    n: int
    label: str
    result: EvalResult
    accepted: bool
    meta: dict


def load_env(path: Path) -> None:
    """Minimal .env loader (stdlib) so live runs pick up keys without python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip())


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seed_prompt(prompt_file: Path) -> str:
    """Start from the existing live prompt if present, else the bot's BASE_SYSTEM."""
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    server_dir = HERE.parent / "yc-voice-agents-hackathon-main" / "server"
    sys.path.insert(0, str(server_dir))
    try:
        from crisis_prompt import BASE_SYSTEM
        return BASE_SYSTEM
    except Exception as e:
        print(f"  (could not import BASE_SYSTEM: {e}; using a minimal seed)")
        return "You are a warm, present crisis-support counselor. Keep replies to 1-2 sentences."


def make_deploy_fn(config: dict, prompt_file: Path):
    """Write the candidate prompt to the seam, and optionally redeploy the agent."""
    deploy_cmd = config.get("agent", {}).get("deploy_cmd")

    def deploy(prompt: str, label: str) -> None:
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")
        if deploy_cmd:
            print(f"  → deploying agent for {label}: {deploy_cmd}")
            subprocess.run(deploy_cmd, shell=True, check=True)

    return deploy


def fmt_bar(value: float, width: int = 30) -> str:
    filled = int(round(value * width))
    return "█" * filled + "·" * (width - filled)


def main() -> None:
    ap = argparse.ArgumentParser(description="CrisisLine prompt auto-improve loop")
    ap.add_argument("--rounds", type=int, default=None, help="optimization rounds (default: config)")
    ap.add_argument("--mock", action="store_true", help="offline mock evaluator + stub optimizer")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    args = ap.parse_args()

    load_env(HERE / ".env")
    config = load_config(Path(args.config))
    rounds = args.rounds if args.rounds is not None else config.get("max_rounds", 3)
    epsilon = config.get("accept_epsilon", 0.0)
    prompt_file = (HERE / config["agent"]["prompt_file"]).resolve()
    runs_dir = HERE / "runs"
    versions_dir = HERE / "prompts"
    runs_dir.mkdir(exist_ok=True)
    versions_dir.mkdir(exist_ok=True)

    llm = LLM()
    if args.mock:
        evaluator = MockEvaluator(config)
        print("MODE: MOCK (offline evaluator + stub optimizer) — for testing/demo fallback")
    else:
        api_key = os.getenv("CEKURA_API_KEY", "")
        base_url = os.getenv("CEKURA_BASE_URL") or config.get("base_url", "https://api.cekura.ai")
        client = CekuraClient(api_key, base_url)
        evaluator = CekuraEvaluator(client, config, make_deploy_fn(config, prompt_file))
        print(f"MODE: LIVE Cekura — optimizer LLM: {llm.provider}")

    history: list[Round] = []

    # --- Round 0: baseline ---
    current = seed_prompt(prompt_file)
    (versions_dir / "system_v0.txt").write_text(current, encoding="utf-8")
    print("\n=== Round 0: baseline ===")
    baseline = evaluator.evaluate(current, "v0")
    history.append(Round(0, "v0", baseline, accepted=True, meta={"mode": "baseline"}))
    best_prompt, best = current, baseline
    print(f"  composite: {best.composite:.3f}  [{fmt_bar(best.composite)}]")
    print(f"  worst metrics: {', '.join(best.worst_metrics[:3])}")

    # --- Improvement rounds ---
    for r in range(1, rounds + 1):
        print(f"\n=== Round {r} ===")
        candidate, meta = propose_improved_prompt(best_prompt, best, r, llm, force_stub=args.mock)
        (versions_dir / f"system_v{r}.txt").write_text(candidate, encoding="utf-8")
        print(f"  optimizer[{meta['mode']}] targeted: {', '.join(meta.get('targeted', []))}")
        if meta.get("rationale"):
            print(f"  rationale: {meta['rationale']}")

        result = evaluator.evaluate(candidate, f"v{r}")
        improved = result.composite >= best.composite + epsilon
        delta = result.composite - best.composite
        if improved:
            best_prompt, best = candidate, result
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(candidate, encoding="utf-8")  # promote
            verdict = f"ACCEPTED (+{delta:.3f})"
        else:
            verdict = f"rejected ({delta:+.3f}) — kept previous best"
        history.append(Round(r, f"v{r}", result, improved, meta))
        print(f"  composite: {result.composite:.3f}  [{fmt_bar(result.composite)}]  {verdict}")

        json.dump(
            {
                "round": r, "label": f"v{r}", "composite": result.composite,
                "accepted": improved, "per_metric": result.per_metric,
                "worst_metrics": result.worst_metrics, "optimizer": meta,
            },
            open(runs_dir / f"round_{r}.json", "w"), indent=2,
        )

    _write_outputs(history, runs_dir, best, best_prompt, prompt_file)


def _write_outputs(history, runs_dir: Path, best: EvalResult, best_prompt: str, prompt_file: Path):
    series = [
        {"round": h.n, "label": h.label, "composite": h.result.composite,
         "accepted": h.accepted, "per_metric": h.result.per_metric}
        for h in history
    ]
    json.dump(series, open(runs_dir / "scores.json", "w"), indent=2)

    lines = ["CrisisLine auto-improve — score climb", "=" * 40]
    for h in history:
        flag = "✓" if h.accepted else "·"
        lines.append(f"  {h.label:>4} {flag} {h.result.composite:.3f}  {fmt_bar(h.result.composite)}")
    start, end = history[0].result.composite, best.composite
    lift = end - start
    lines += [
        "=" * 40,
        f"  baseline {start:.3f} → best {end:.3f}   (lift +{lift:.3f}, {lift / start * 100:.1f}%)"
        if start else f"  best {end:.3f}",
        f"  best prompt written to: {prompt_file}",
    ]
    summary = "\n".join(lines)
    (runs_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"\nScore series for plotting: {runs_dir / 'scores.json'}")


if __name__ == "__main__":
    main()
