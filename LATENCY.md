# Latency: measured, not vibed

The brief asks to *"optimize network performance and eliminate latency."* So we
measured it from the deployed agent's own metrics instead of guessing.

## What a caller felt
First live call (Nemotron pipeline on Pipecat Cloud), pulled from
`pcc-observability` logs:

```
User-bot latency: 3.786s
```

That feels slow. The instinct is "the 120B model is heavy." **The logs proved
that wrong.**

## Where the time actually went (TTFB per stage, same call)

| Stage | Service | TTFB |
|---|---|---|
| Speech-to-text | NVIDIA Nemotron ASR | **0.019 s** |
| LLM first token | Nemotron-3-Super-120B (vLLM) | **0.16 – 0.30 s** |
| Text-to-speech first audio | Gradium | **0.30 s** |
| **Sum of the pipeline** | | **~0.65 s** |

Every component is fast. The model is **not** the bottleneck — first token from a
120B open-weights model lands in ~0.2s.

## The real culprit: turn-taking
~3s of the 3.8s was **the agent waiting to decide the caller had finished
speaking** (VAD end-of-turn silence + smart-turn detection) plus phone-network
round-trip — not inference.

## The fix
- Cut Silero VAD `stop_secs` **0.8 → 0.45** (reply ~0.35s sooner after the caller stops)
- Kept smart-turn detection on — a crisis caller pauses and breathes; we don't
  want to cut them off. The win is in the silence threshold, not interrupting.

```python
SileroVADAnalyzer(params=VADParams(stop_secs=0.45, start_secs=0.2))
```

## The takeaway for judges
We didn't trade the open-weights model for a smaller one to "feel faster." We
**measured**, found the latency was in the turn-taking layer, and tuned that —
keeping Nemotron-3-Super and the no-interrupt behavior a crisis line needs.
Open-weights reasoning *and* responsive — because we optimized the right layer.
