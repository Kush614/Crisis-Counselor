# CrisisLine Auto-Improve Loop

The closed feedback loop that turns Cekura evaluation data back into a better
counselor — the "best system, not best voice" story for the hackathon.

```
 run scenarios on the Pipecat agent ─► per-metric scores + failing transcripts
            ▲                                          │
            │                              optimizer LLM rewrites the prompt
            │                              (targets the worst metrics)
   promote if composite improved ◄── re-evaluate ◄── write system_v{n}.txt
```

## Files
| File | Role |
|---|---|
| `config.json` | Cekura base URL, scenario IDs, metric weights, rounds, accept gate, the prompt-file seam |
| `cekura_client.py` | REST client (stdlib) — run scenarios, poll runs, get results |
| `evaluator.py` | `CekuraEvaluator` (live) + `MockEvaluator` (offline) → a normalized `EvalResult` |
| `optimize.py` | Proposes an improved prompt from the worst metrics + failing evidence |
| `llm.py` | Optimizer LLM (Anthropic/OpenAI, stub fallback) |
| `run_loop.py` | Orchestration: rounds, accept/reject gate, logging, score-climb output |

## Run it offline (no keys) — proves the loop + demo fallback
```bash
cd optimizer
python run_loop.py --mock --rounds 4
```
Outputs land in `optimizer/runs/`: `scores.json` (plot this), `round_<n>.json`, `summary.txt`.

## Run it live against Cekura
1. **Deploy the agent** to Pipecat Cloud (`pc cloud deploy`) and connect it in
   Cekura (provider **Pipecat**).
2. **Create the 8 evaluators** in Cekura (passive ideation, active intent, …) and
   the LLM-judge **metrics** (safety adherence, empathy-first, brevity, …). Put
   the numeric scenario IDs into `config.json` → `scenarios[].id`.
3. **Set keys:**
   ```bash
   export CEKURA_API_KEY=...
   export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY — for the optimizer
   ```
4. **Point the bot at the evolving prompt** so accepted prompts go live:
   ```bash
   export CRISIS_PROMPT_FILE=.../server/optimizer_prompts/system_current.txt
   ```
   If you redeploy between rounds, set `config.json` → `agent.deploy_cmd` (e.g.
   `"pc cloud deploy"`); otherwise a locally-run agent re-reads the file each call.
5. **Go:**
   ```bash
   python run_loop.py --rounds 3
   ```

## The accept/reject gate (anti-gaming)
A new prompt is promoted only if its composite score is ≥ the current best
(`accept_epsilon` in config). The same scenario set every round is the frozen
regression set, so the optimizer can't win one metric by breaking another.

## Plot the climb (demo)
```python
import json, matplotlib.pyplot as plt
s = json.load(open("runs/scores.json"))
plt.plot([r["round"] for r in s], [r["composite"] for r in s], "-o")
plt.xlabel("round"); plt.ylabel("composite counseling score"); plt.show()
```
