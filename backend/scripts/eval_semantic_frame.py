#!/usr/bin/env python3
"""Score the semantic layer's understanding, per language (T05.13, AC05.08).

    # Free: the deterministic backstop only (no model, no cost).
    python scripts/eval_semantic_frame.py --backstop-only
    python scripts/eval_semantic_frame.py --backstop-only --split held_out --lang es

    # SPENDS MODEL TOKENS through llm_gateway (task="fast"): one structured
    # call per case, retried once on an unusable answer. Owner approval for
    # the spend is required first (CONTRACT C4); the flag is the record of it.
    python scripts/eval_semantic_frame.py --approve-spend --split held_out --out /tmp/frames.json

Every metric is reported for English, Spanish and mixed requests separately
(`by_lang`), and overall. See `semantic_eval` for what each metric means.

What this does NOT measure: access control (AC05.03 has its own real-
PostgreSQL test), tool execution (07/08) or factual grounding (06). The
``dev`` split is the one the backstop lexicon was written against; only
``held_out`` is a held-out number, and even that was written by the same
author as the lexicon.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ORACLE_SKIP_DOTENV", "1")

import semantic_eval  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=semantic_eval.DATASET)
    parser.add_argument("--split", choices=("dev", "held_out", "all"), default="all")
    parser.add_argument("--lang", choices=semantic_eval.GROUPS, action="append",
                        help="repeatable; default every language")
    parser.add_argument("--backstop-only", action="store_true",
                        help="score the deterministic layer alone (no model, no cost)")
    parser.add_argument("--approve-spend", action="store_true",
                        help="the owner approved model spend for this run")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    cases = semantic_eval.load_cases(
        args.dataset, split=None if args.split == "all" else args.split, langs=args.lang,
    )
    if not cases:
        print("no cases match", file=sys.stderr)
        return 2

    if args.backstop_only:
        result = semantic_eval.score_backstop(cases)
        mode = "backstop_only"
    else:
        if not args.approve_spend:
            print(
                f"refused: scoring the model parser spends model tokens ({len(cases)} cases, "
                "1-2 calls each). Get owner approval, then pass --approve-spend; or run "
                "--backstop-only, which costs nothing.",
                file=sys.stderr,
            )
            return 2
        import llm_gateway  # noqa: PLC0415

        result = asyncio.run(semantic_eval.run(cases, gateway=llm_gateway,
                                               concurrency=args.concurrency))
        mode = "model"

    report = {
        "mode": mode,
        "dataset": str(args.dataset.name),
        "split": args.split,
        "cases": len(cases),
        "conditions": {
            "now": semantic_eval.NOW.isoformat(),
            "timezone": semantic_eval.ZONE,
            "access": "every offered record treated as visible (understanding only)",
        },
        **result,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
