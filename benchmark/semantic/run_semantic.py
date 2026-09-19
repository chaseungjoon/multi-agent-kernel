"""Run the Wave 20 semantic-conflict corpus and print the comparison table.

python benchmark/semantic/run_semantic.py            # markdown table
python benchmark/semantic/run_semantic.py --json     # one JSON object per shape
python benchmark/semantic/run_semantic.py --no-gates # default config only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic.evaluate import ScenarioResult, evaluate  # noqa: E402
from semantic.scenarios import SCENARIOS  # noqa: E402

_SHAPE_10 = (
    "| 10 | out-of-store artifacts | not representable | not representable | — | — |"
)


def _row(result: ScenarioResult) -> str:
    mak = result.mak.outcome
    if not result.mak.resolved and mak.startswith("detected"):
        mak += "; fix-up wave" if result.mak.fixups else "; unresolved"
    if result.mak_gated is not None:
        mak += f" / with {', '.join(result.scenario.needs)}: {result.mak_gated.outcome}"
    return (
        f"| {result.scenario.shape} | {result.scenario.name} | {mak} | "
        f"{result.worktree.outcome} | {result.false_positives} | {result.extra_calls} |"
    )


def main(argv: list[str] | None = None) -> int:
    """Evaluate every scenario and print the table (or JSON lines)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON lines")
    parser.add_argument("--no-gates", action="store_true", help="skip gated runs")
    args = parser.parse_args(argv)
    results = [evaluate(s, with_gates=not args.no_gates) for s in SCENARIOS]
    if args.json:
        for r in results:
            print(json.dumps({
                "shape": r.scenario.shape,
                "name": r.scenario.name,
                "mak": r.mak.outcome,
                "mak_resolved": r.mak.resolved,
                "mak_mechanisms": list(r.mak.mechanisms),
                "mak_gated": r.mak_gated.outcome if r.mak_gated else None,
                "worktree": r.worktree.outcome,
                "false_positives": r.false_positives,
                "extra_calls": r.extra_calls,
            }))
        return 0
    print("| shape | scenario | MAK | worktrees | false positives | extra calls |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(_row(r))
    print(_SHAPE_10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
