# MAK benchmark — shared-memory kernel vs. git worktrees

A fair, reproducible comparison of two ways to run several coding agents on one
codebase at the same time:

- **MAK** — the shared-memory kernel in this repo. Agents edit one working directory;
  a node-level lock table serializes edits to contended symbols.
- **Traditional** — the git-worktree model. Each agent gets its own branch + worktree,
  works in isolation, and the branches are merged at the end.

Both run the **same workload** with the **same agents** (same models, same
per-operation prompt, same task assignment). This controls the workload and model
configuration; individual model responses can still differ between runs.

## What it measures

- **Implementation time** — wall-clock to produce the finished code.
- **Tokens spent** — total input + output tokens across all model calls (including any
  the traditional side spends resolving merge conflicts).
- **Accuracy** — fraction of the target project's test suite that passes afterward.

It also reports the structural driver of the difference: **merge conflicts** and
**conflict-resolution calls**.

## The workloads

Four targets covering two contention shapes — pick one with
`--project basic|2|3|4|all` (default `all`):

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
- **`4`** (`project_template_4/`) — a multi-tenant background-job service with
  **24 tasks**, six feature modules, three shared tables, **152 acceptance tests**,
  and one Opus 5 planner coordinating three Opus 5 workers. Details below.

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
In all four targets, feature/module files are assigned one-agent-per-module so they
merge cleanly — conflicts are isolated to exactly the contended symbols.

## Template 4: operating a multi-tenant job service

`project_template_4/` models a release that completes an existing background-job
service. This is the kind of work teams split across backend, reliability, and
platform engineers: each feature has its own module, but API routes, event handlers,
and policy registrations change together.

| Feature module | Engineering work |
|---|---|
| `tenancy` | Tenant validation, role authorization, isolated reads, active-job quotas |
| `submission` | Canonical JSON, tenant-scoped idempotency, job creation, payload digests |
| `scheduling` | Bounded exponential backoff, ready-job selection, HTTP Retry-After, retry policy |
| `leasing` | Worker claims, heartbeat renewal, expiry detection, crashed-worker recovery |
| `lifecycle` | Completion, bounded retries, dead-letter handling, cancellation, replay |
| `operations` | Cursor pagination, state counts, retention selection, backlog age |

**24 tasks; 152 independently scored tests:** 112 contract/boundary checks,
30 shared-registration checks, and 10 multi-feature workflows. Workflows exercise
submission → claim → completion, retry → dead letter → replay, stale-worker
rejection after recovery, tenant isolation, cancellation, idempotency conflicts,
pagination, backpressure, backlog metrics, and retention. Inputs are immutable
records; time is injected, so the oracle needs no network, database, or sleeps.
Each task touches zero, one, or two of three shared tables (`routes`, `events`,
`policies`). Tables build local dictionaries on lookup, avoiding mutable global state
and allowing missing registrations to fail individual tests without breaking imports.

### One planner, three workers

Template 4 defaults to **one `anthropic:claude-opus-5` planner and three distinct
`anthropic:claude-opus-5` workers**. The planner receives the public data model,
function contracts, and shared-table targets. It assigns entire modules to workers
and writes implementation guidance. The harness validates complete, unique module
ownership, valid worker indices, nonempty guidance, and work for every worker.
Literal line breaks in JSON guidance are accepted; ownership and schema checks
still apply. Invalid or truncated plans get at most three attempts with validation
feedback. Exhausting those attempts stops the run before implementation; no
fabricated plan is substituted. Anthropic planner responses have an 8,192-token
output budget (workers retain 2,048).

Each repetition produces one validated plan and reuses it for both MAK and
worktrees. All planning attempts count toward the planner time, tokens, and calls. Real workers receive the same contracts, record definitions, and
planner guidance; they never receive reference implementations or oracle answers.
The worker models are independently called on each side, including the worktree
side's merge-resolution calls. `--models` overrides worker defaults;
`--planner-model provider:model` overrides the planner. Defaults for other targets
remain their existing provider mix. `--agents` defaults to three; explicit worker
lists determine the worker count, with at most six workers for this workload.

**Accounting:** each side's headline includes the planner's measured time, tokens,
and call count once. Separate planner rows show the included amounts. This makes
the two totals comparable; adding them together would double-count the one shared
planning phase. The worker-call table excludes the planner. Every repetition's plan,
usage, and guidance are saved in `benchmark/.last_run.4.json`; `--keep` also retains
`benchmark/.runs/4/plan-N.json` and the implemented project copies. Raw responses,
usage, and validation errors are written immediately to
`benchmark/.runs/4/planner-N/attempt-M.json`, including on failure, and successful
plans retain all attempts in `.last_run.4.json`. The next benchmark invocation
clears `.runs/`, so copy failed-run diagnostics before rerunning if needed.

**Measurement scope:** this extends the existing controlled function-edit harness.
The benchmark planner assigns predefined tasks; it does not exercise MAK's production
planner's open-ended decomposition. Workers implement one function per call, while
registrations are deterministic harness edits. Integration is checked after all edits;
this does not measure live queue throughput, database transactions, network delivery,
or dependency-driven collaboration between workers. MAK time is measured execution
wall time; the existing worktree baseline executes worker calls sequentially and
reports a simulated parallel time (maximum worker call time plus measured sequential
merge time). Setup and oracle execution are outside both implementation timings.
Score differences can come from implementations as well as merges. Repeat real runs
before drawing conclusions; mock results only validate the harness.

The checked-in Template 4 results are a **mock self-test**, not an Opus 5 evaluation.

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
- **Parallel timing model** — the traditional implementation phase simulates parallel
  workers by charging `max` over each agent's call time (not the sum); the sequential merge+resolve phase is added on top. MAK is charged its
  real wall-clock end to end.

## Run it yourself

From the repository root:

```bash
# The accuracy oracle is part of the development dependencies:
python3 -m pip install -e '.[dev]'

# Keyless self-test — proves the harness runs end to end (not representative numbers):
python benchmark/run_benchmark.py --mode mock

# The real benchmark — needs API keys for the models you choose (runs all targets):
export ANTHROPIC_API_KEY=sk-...   # and/or OPENAI_API_KEY, GEMINI_API_KEY
python benchmark/run_benchmark.py --mode real

# Just one target (the heavy one, or the real-world one):
python benchmark/run_benchmark.py --mode real --project 2
python benchmark/run_benchmark.py --mode real --project 3

# Template 4: one Opus 5 planner + three Opus 5 workers (ANTHROPIC_API_KEY):
python benchmark/run_benchmark.py --mode real --project 4

# Explicit equivalent configuration:
python benchmark/run_benchmark.py --mode real --project 4 \
  --planner-model anthropic:claude-opus-5 \
  --models anthropic:claude-opus-5 anthropic:claude-opus-5 anthropic:claude-opus-5

# Template 4 keyless verification; retains output for inspection:
python benchmark/run_benchmark.py --mode mock --project 4 --keep

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

To extend Template 4, edit `harness/template4_spec.py` (contracts/references/checks)
and `harness/template4_workflows.py` (integration scenarios), then regenerate:

```bash
python benchmark/tools/gen_template4.py
python -m pytest tests/test_benchmark_template4.py -q
```

Generation never evaluates references to compute expected answers. The regression
suite checks reproducibility, that all 152 baseline checks fail, planner validation,
legacy workload compatibility, and successful mock runs on both coordination models.
Template 4 test alarms run on POSIX; on other hosts use an external process timeout.

## Results

> Model configuration is recorded separately for each target. Use `--models` to
> override the workers and `--planner-model` to override Template 4's planner.

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


---

### Template 4 — 24 operations, 6 modules

_Last run: 2026-09-15T17:07:28 · mode `real` · 3 agents · mean of 10 runs._

> **Mode: `real`.** 3 agents (claude-opus-5, claude-opus-5, claude-opus-5) implementing 24 operations (verified by 152 tests). Figures are the **mean of 10 runs** (per-run breakdown below). Planner: `anthropic:claude-opus-5`; one plan reused for both sides, with its cost included equally in both totals.

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 89.40s | 98.51s |
| Total tokens | 36,366 | 40,787 |
| Model calls | 25 | 31 |
| Accuracy (tests passed) | 151.9/152 (100%) | 151.9/152 (100%) |
| Registry merge conflicts | 0 | 6 |
| Conflict-resolution calls | 0 | 6 |
| Planner time (included above) | 29.26s | 29.26s |
| Planner tokens (included above) | 6,132 | 6,132 |
| Planner calls (included above) | 1 | 1 |

**Reading the numbers:**

- **Accuracy:** MAK 151.9/152; Traditional 151.9/152. The oracle covers function contracts, shared wiring, and complete service workflows.
- **Coordination:** MAK 0 merge conflicts; Traditional 6 conflicted files and 6 resolution calls across three shared tables.
- **Planner:** the same validated ownership and guidance are reused by both sides; each total includes the measured planner cost once.
- **Resources:** MAK 36,366 tokens / 89.40s; Traditional 40,787 tokens / 98.51s. Traditional time uses simulated parallel worker calls plus the measured merge phase; MAK time measures actual execution. Model outputs can differ, so score differences alone do not identify a merge failure.

See [STATS.md](STATS.md) for the full breakdown.

<!-- RESULTS:END -->
