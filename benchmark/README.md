# MAK benchmark — shared-memory kernel vs. git worktrees

A fair, reproducible comparison of two ways to run several coding agents on one
codebase at the same time:

- **MAK** — the shared-memory kernel in this repo. Agents edit one working directory;
  a node-level lock table serializes edits to contended symbols.
- **Traditional** — the git-worktree model. Each agent gets its own branch + worktree,
  works in isolation, and the branches are merged at the end.

Both run the **same workload** with the **same agents** (same models, same
per-operation prompt, same task assignment). The *only* difference is the coordination
model — so any difference in the results is attributable to that.

## What it measures

- **Implementation time** — wall-clock to produce the finished code.
- **Tokens spent** — total input + output tokens across all model calls (including any
  the traditional side spends resolving merge conflicts).
- **Accuracy** — fraction of the target project's test suite that passes afterward.

It also reports the structural driver of the difference: **merge conflicts** and
**conflict-resolution calls**.

## The workloads

Three targets covering two contention shapes — pick one with
`--project basic|2|3|all` (default `all`):

- **`basic`** (`project_template/`) — a `toolkit` library with **9 operations** across
  three modules (`strings`, `numbers`, `sequences`); a 30-test oracle.
- **`2`** (`project_template_2/`) — a much larger, harder `toolkit` with **90 operations**
  across nine modules (`strkit`, `numkit`, `seqkit`, `dictkit`, `datekit`, `mathkit`,
  `parsekit`, `setkit`, `codekit`) — utility functions in the spirit of
  `boltons`/`more-itertools`/`toolz` (Levenshtein distance, Roman numerals, calendar math,
  prime sieves, small parsers, set algebra, ciphers); a 270-test oracle. It is generated
  from `harness/template2_spec.py` by `tools/gen_template2.py`, so its stubs, reference
  implementations, and tests cannot drift.
- **`3`** (`project_template_3/`) — the **real-world contention** target: a small
  storefront backend (`app`) with **58 feature tasks** across eight feature modules
  (`accounts`, `catalog`, `cart`, `orders`, `payments`, `shipping`, `reviews`,
  `search`) and **four cross-cutting shared tables** — `routes` (URL dispatch),
  `events` (event handlers), `errors` (error-code catalog), `settings` (config
  defaults) — the files real feature teams collide on. Each task registers into
  **zero, one, or two** of the tables (a handful legitimately touch two — a route
  *and* its error code — exercising MAK's atomic multi-node lock claims); a 148-test
  oracle. Generated from `harness/template3_spec.py` by `tools/gen_template3.py`.

**Two contention shapes.** `basic` and `2` are *maximally contended*: every operation
adds one line to a single shared dispatch table, `registry._register_all` — the one
symbol every agent must touch. That isolates the coordination difference perfectly
(under MAK a node-level write lock serializes those edits and none are lost; under
worktrees every merge after the first collides there), but it also serializes MAK's
whole run, so the worktrees win on wall-clock — an honest, deliberate trade noted in
the results.

`3` is *partially contended* — the shape of real feature work. Most tasks touch only
their own module (fully parallel under both models); the contention that remains is
spread across four shared `_register_all` tables. Under MAK, only same-table edits
briefly serialize and everything else proceeds in parallel — while the worktree side
now collides on **several files per merge** (each conflicted file its own resolution
call, its own chance to drop a registration) plus its sequential merge phase. This is
the workload where MAK's parallelism and its zero-conflict property show *together*.
In all three targets, feature/module files are assigned one-agent-per-module so they
merge cleanly — conflicts are isolated to exactly the contended symbols.

## Fairness controls

- **Same agents, same models** for both sides (configure with `--models`).
- **Same agent layer** (`harness/agents.py`) — identical prompts and identical
  deterministic registry edit; the model's only creative job is the function body.
- **Same assignment** — operation → agent mapping is identical for both runners.
- **Same oracle** — accuracy is the same per-target test suite run the same way.
- **Malformed output isolated, not fatal** — a garbled agent response (e.g. an
  unparseable function) is rejected on both sides (MAK drops the staged node and retries;
  the worktree runner refuses to splice unparseable Python), so one bad call costs that
  operation its tests instead of crashing the run.
- **Parallel timing model** — the traditional side's agents work concurrently, so its
  implementation phase is charged as `max` over agents of that agent's call time (not
  the sum); the sequential merge+resolve phase is added on top. MAK is charged its
  real wall-clock end to end.

## Run it yourself

From the repository root:

```bash
# The accuracy oracle is part of the development dependencies:
python3 -m pip install -e '.[dev]'

# Keyless self-test — proves the harness runs end to end (not representative numbers):
python benchmark/run_benchmark.py --mode mock

# The real benchmark — needs API keys for the models you choose (runs both targets):
export ANTHROPIC_API_KEY=sk-...   # and/or OPENAI_API_KEY, GEMINI_API_KEY
python benchmark/run_benchmark.py --mode real

# Just one target (the heavy one, or the real-world one):
python benchmark/run_benchmark.py --mode real --project 2
python benchmark/run_benchmark.py --mode real --project 3

# Average over several runs (the published Template 2 numbers are --repeat 10):
python benchmark/run_benchmark.py --mode real --project 3 --repeat 10

# Pick your own agents (provider:model), same set used for both sides:
python benchmark/run_benchmark.py --mode real \
  --models anthropic:claude-sonnet-5 openai:gpt-5.6-sol gemini:gemini-3.5-flash
```

Each run writes its results into the **Results** section below and the full breakdown
into [STATS.md](STATS.md); with `--repeat N` the headline is the mean of N runs and
STATS.md gains a per-run breakdown table. Working copies live under `benchmark/.runs/`
(gitignored); pass `--keep` to inspect them. A per-call liveness line is printed to
stderr so a long sweep is visibly progressing.

## Results

> All three agents are Claude. The harness takes any provider mix via `--models`; supply your own keys
> and re-run to compare across models.

<!-- RESULTS:START -->

### Basic toolkit (9 ops) — 9 operations, 3 modules

_Last run: 2026-06-09T19:13:39 · mode `real` · 3 agents._

> **Mode: `real`.** 3 agents (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6) implementing 9 operations (verified by 30 tests).

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 20.37s | 11.64s |
| Total tokens | 2,052 | 3,192 |
| Model calls | 9 | 11 |
| Accuracy (tests passed) | 30/30 (100%) | 30/30 (100%) |
| Registry merge conflicts | 0 | 2 |
| Conflict-resolution calls | 0 | 2 |

**Reading the numbers:**

- **Tokens:** MAK spent **36% fewer** (2,052 vs 3,192) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** tied at 100%. These tasks are small and the resolver merged the registry correctly *this time*; the structural risk MAK removes — a dropped or garbled registration — is what bites on larger tasks or weaker resolvers.
- **Time:** the worktree run was faster here (11.6s vs 20.4s): *every* task contends on the one shared registry node, so MAK serializes them while the worktrees run fully in parallel and reconcile afterwards. On a workload with more independent work, MAK parallelizes that part too — this benchmark deliberately maximizes contention.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2** conflicted files, each an extra resolution call.


---

### Template 2 (90 ops) — 90 operations, 9 modules

_Last run: 2026-06-14T19:03:31 · mode `real` · 3 agents · mean of 10 runs._

> **Mode: `real`.** 3 agents (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6) implementing 90 operations (verified by 270 tests). Figures are the **mean of 10 runs** (per-run breakdown below).

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 226.54s | 99.52s |
| Total tokens | 18,339 | 23,760 |
| Model calls | 91 | 92 |
| Accuracy (tests passed) | 253.1/270 (94%) | 251.6/270 (93%) |
| Registry merge conflicts | 0 | 2 |
| Conflict-resolution calls | 0 | 2 |

**Reading the numbers:**

- **Tokens:** MAK spent **23% fewer** (18,339 vs 23,760) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** MAK higher — MAK 94% vs Traditional 93% (Traditional lost work in the merge).
- **Time:** the worktree run was faster here (99.5s vs 226.5s): *every* task contends on the one shared registry node, so MAK serializes them while the worktrees run fully in parallel and reconcile afterwards. On a workload with more independent work, MAK parallelizes that part too — this benchmark deliberately maximizes contention.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2** conflicted files, each an extra resolution call.


---

### Template 3 (real-world, 58 tasks) — 58 operations, 8 modules

_Last run: 2026-07-19T17:34:41 · mode `real` · 3 agents · mean of 10 runs._

> **Mode: `real`.** 3 agents (claude-sonnet-5, gpt-5.6-sol, gemini-3.5-flash) implementing 58 operations (verified by 148 tests). Figures are the **mean of 10 runs** (per-run breakdown below).

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 57.07s | 74.12s |
| Total tokens | 13,911 | 16,291 |
| Model calls | 44 | 44 |
| Accuracy (tests passed) | 111.4/148 (75%) | 93.7/148 (63%) |
| Registry merge conflicts | 0 | 4 |
| Conflict-resolution calls | 0 | 4 |

**Reading the numbers:**

- **Tokens:** MAK spent **15% fewer** (13,911 vs 16,291) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** MAK higher — MAK 75% vs Traditional 63% (Traditional lost work in the merge).
- **Time:** MAK was faster (57.1s vs 74.1s) — contention is spread over 4 shared tables, so most tasks proceed in parallel while the worktree side still pays a sequential merge-and-resolve phase.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **4** conflicted files, spread across the shared tables, each an extra resolution call.

See [STATS.md](STATS.md) for the full breakdown.

<!-- RESULTS:END -->
