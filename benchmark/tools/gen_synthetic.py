"""Generate a standalone synthetic project used by the Wave 21 sweep."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1]
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

from harness.synthetic_spec import (  # noqa: E402
    SyntheticSpec,
    build_workload,
    from_profile,
    write_project,
)


def main() -> None:
    """Parse generation parameters and write a project tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--modules", type=int, default=8)
    parser.add_argument("--tasks", type=int, default=40)
    parser.add_argument("--shared-tables", type=int, default=4)
    parser.add_argument("--popularity", choices=("uniform", "zipf"), default="uniform")
    parser.add_argument("--zipf-exponent", type=float, default=1.2)
    parser.add_argument("--assignment", default="round_robin")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--from-profile", type=Path)
    args = parser.parse_args()
    spec = SyntheticSpec(
        modules=args.modules,
        tasks=args.tasks,
        shared_tables=args.shared_tables,
        popularity=args.popularity,
        zipf_exponent=args.zipf_exponent,
        assignment_policy=args.assignment,
        seed=args.seed,
    )
    if args.from_profile:
        spec = from_profile(spec, args.from_profile)
    generated = build_workload(spec)
    write_project(args.output, generated)
    print(f"[synthetic] wrote {args.output} ({args.tasks} tasks)")


if __name__ == "__main__":
    main()
