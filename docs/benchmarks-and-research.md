# Benchmarks and research

> [Contributing guide](../CONTRIBUTING.md)

Each benchmark and study keeps its full documentation (method, fairness
controls, commands, and results) next to its code. This page says what each one
measures, how to run it without spending tokens, and what to keep in mind when
changing it.

| Suite | Measures | Keyless run |
|---|---|---|
| [MAK vs. git worktrees](#benchmark-mak-vs-git-worktrees) | the kernel against the worktree model on contended targets | `python benchmark/run_benchmark.py --mode mock` |
| [Planner input](#planner-input-offline) | the size of what the planner is sent | `python benchmark/tools/planner_input.py` |
| [Semantic conflict corpus](#semantic-conflict-corpus) | whether each coordination model catches ten conflict shapes | `python benchmark/semantic/run_semantic.py` |
| [Simulated-agent scaling](#simulated-agent-scaling-sweep) | scaling with simulated model calls | `python benchmark/sweep.py --config benchmark/sweeps/smoke.yaml` |
| [Real-world contention](#research-real-world-contention) | how often real concurrent changes collide per file vs. per node | see the study's README |

## Benchmark: MAK vs. git worktrees

[`benchmark/README.md`](../benchmark/README.md) (per-run detail in
[`benchmark/STATS.md`](../benchmark/STATS.md)) is a head-to-head between MAK and the
git-worktree model on four generated targets (Basic, Template 2, Template 3,
Template 4). Both sides use the same agents, prompts, task assignment and test
oracle; only the coordination model differs. Every target shares one registry
that every task must append to, so the workloads are **maximally contended**.

When you change it:

- Targets are generated from `benchmark/harness/template*_spec.py` by
  `benchmark/tools/gen_template*.py`; edit the spec, never the generated files.
- The MAK side is given an **oracle plan** (exact targets, `changes_api=False`,
  precomputed registry keys) and the worktree side is a **simulated** pipeline.
  The numbers therefore measure the kernel, not the planner or agent quality;
  label them that way. Wave 33 in [`TASKS.md`](../TASKS.md) adds end-to-end and
  real-baseline arms.
- `python benchmark/run_benchmark.py --mode mock` is the keyless self-test;
  `--render-only` regenerates the reports without spending tokens.

## Planner input (offline)

`benchmark/tools/planner_input.py` measures what MAK's real `Planner` sends —
same inventory view, retrieval, rounds and prompt layout as a session — against a
recording fake LLM, so it costs nothing. Inputs are MAK's own `mak/`, the four
benchmark templates and synthetic repositories of 10, 100 and 1,000 files
(`tests/planner/synthetic_repo.py`); strategies `oneshot` and `auto`. It writes
`benchmark/results/planner_input.json` and prints a table
([`benchmark/README.md`](../benchmark/README.md) quotes it).
`tests/test_benchmark_planner_input.py` runs it on synthetic-10. It measures
prompt size, not plan quality; plan quality with real models is Wave 33's.

```bash
python benchmark/tools/planner_input.py
```

## Semantic conflict corpus

[`benchmark/semantic/`](../benchmark/semantic/) measures whether each coordination
model **catches** a semantic conflict. Each scenario is a tiny project, two
scripted edits A and B (`ScriptedAgent`, with a hook that holds B until A commits
when they run concurrently), and an oracle that must pass on the base, on A
alone, and on B alone, and **fail** on the naive combination
(`evaluate.validate`). `evaluate.run_mak` drives a real `Session`;
`evaluate.run_worktrees` merges the two single-edit states.

| shape | scenario | MAK | worktrees |
|---|---|---|---|
| 1 | stale read (write skew) | detected (commit) | missed |
| 2 | signature change vs. new call | detected (wave end) | missed |
| 3 | behaviour change, same signature | missed; detected with `impact_tests` | detected (tests) |
| 4 | deletion / rename | detected (wave end) | missed |
| 5 | override against a changed base | detected (wave end) | missed |
| 6 | duplicate registration key | detected (commit) | detected (textual) |
| 7 | order-dependent table | **prevented** | detected (textual) |
| 8 | new required field vs. new construction | detected (wave end) | missed |
| 9 | duplicate implementation | detected (wave end) | missed |
| 10 | out-of-store artifacts (config, SQL, docs) | not representable | not representable |

Zero false positives across single-edit runs, and at most one extra agent call
per detected shape. `tests/test_semantic_corpus.py` gates both the table and the
corpus's validity contract.

```bash
python benchmark/semantic/run_semantic.py          # markdown table
python benchmark/semantic/run_semantic.py --json   # one JSON object per shape
```

## Simulated-agent scaling sweep

`benchmark/sweep.py` drives the real kernel and real git with simulated model
calls to measure scaling without spending tokens. Its design, calibration and
threats to validity are in [`benchmark/sim/README.md`](../benchmark/sim/README.md)
and the "Simulated agent scaling" section of `benchmark/README.md`. Two rules:
generated targets come from `benchmark/harness/synthetic_spec.py` (keep additions
there so target code and oracle cannot drift), and the bundled
`profiles/default.json` is a placeholder — never describe it as calibrated.

```bash
python benchmark/sweep.py --config benchmark/sweeps/smoke.yaml   # keyless
```

## Research: real-world contention

[`research/contention_study/`](../research/contention_study/) measures how often real concurrent
changes in six large Python projects touch the same file versus the same MAK
node. Its findings, method and threats to validity are in
[`CONTENTION_STUDY.md`](../research/contention_study/CONTENTION_STUDY.md); setup, commands
and the pipeline stages are in
[`research/contention_study/README.md`](../research/contention_study/README.md); generated tables are
in `research/contention_study/data/RESULTS.md` — check that file before quoting any number.

Rules for changing the study:

- Keep the kernel read-only. The study imports
  `mak.node_store.ingestion.parse_file_into_fragments` so a measured node is
  exactly a node MAK would lock; a question that needs `mak/` changed is a
  separate wave.
- Pairs require **base overlap**, not just lifetime overlap, and merges are
  judged against a shared base (`mining/rebase.py`). Never use
  `git merge-tree A B` — it charges mainline commits to one PR.
- Keep the failure taxonomy distinct: source conflict, rebase conflict, invalid
  Git operation, missing head, unparseable file.
- Add columns through the cache migration layer; record excluded populations in
  named buckets.
- Regenerate `data/RESULTS.md`, `data/results.json`, profiles, CSVs and plots
  from code; never hand-edit aggregates.
- Research dependencies stay in `research/contention_study/.venv`; never add NumPy or
  Matplotlib to the kernel's `pyproject.toml`. The study has its own gates:
  `./run.sh pytest tests -q`, `ruff check .`, and strict mypy on `mining`.

---

[Contributing guide](../CONTRIBUTING.md)
