# AI32-08 — real-model run of the semantic-frame suite (2026-11-20)

Owner-approved spend (2026-11-20). Command: `backend/scripts/eval_semantic_frame.py --approve-spend --split all`
through `llm_gateway` (task `fast`), every case through the product's own enforcement
(`frame_constraints` + backstop) and tool check.

- Code: `main` at `a7d4caf`. Dataset: `backend/eval_data/semantic_frames_v1.jsonl`, sha256 prefix `3769ec8d092ba3b0`,
  129 cases (en 31, es 65, mixed 33; dev 99, held_out 30). Clock and zone fixed by the harness (`conditions`).
- Parser deadline: the product's 8 s (`semantic_frame.FRAME_TIMEOUT_S`); a slower answer counts as an abstention.

| Run | Model (fast tier) | parsed | intent | constraint exact / recall | preservation | unauthorized | p50 / p95 |
|---|---|---|---|---|---|---|---|
| `configured-2026-11-20.json` | as configured: Foundry (unreachable — Azure subscription disabled) → Fireworks `glm-5p2-fast` ("Model not found", retired) | 0.0% | — | — | 1.0 (backstop only) | 0 | — |
| `gpt-oss-120b-2026-11-20.json` | `accounts/fireworks/models/gpt-oss-120b` | 80.6% | 0.760 | 0.904 / 0.901 | 1.0 | 0 | 4.8 s / 12.5 s |
| `deepseek-v4p1-flash-ultrafast-2026-11-20.json` | `accounts/fireworks/routers/deepseek-v4p1-flash-1017-ultrafast` | 96.9% | 0.744 | 0.944 / 0.978 | 1.0 | 0 | 2.3 s / 4.5 s |

Per-language numbers are in each file (`by_lang`). Probed and rejected for the fast tier: `glm-5p3-fast` and `kimi-k3`
(timed out at 8 s), `glm-5p3-flash` (timed out), `minimax-m3p1-flash` (unavailable).

What it does NOT show:
- No case expects a clarifying question, yet clarification was 0.936 (deepseek) and 0.317 (gpt-oss) of parsed frames:
  mostly "which Dana?" for people the case lists no records for, and some over-asking ("what should the draft say?").
  Prompt tuning, and cases with known records, are the next step.
- Cost: the harness does not record tokens; the provider's billing shows the actual spend (expected well under $2 for
  both full runs). Latency is the parser's own measurement.
- Understanding only: not chat, tool, voice or billing journeys end to end (those need their own real-model runs).
- No go-live threshold is written down for AC05.08; preservation 1.0 and 0 unauthorized actions hold for every run,
  including the one where no model answered (the deterministic backstop).
