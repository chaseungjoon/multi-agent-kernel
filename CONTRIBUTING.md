# Contributing to the Multi Agent Kernel (MAK)

Welcome, and thank you for considering a contribution to MAK.

This document is the **single, self-contained reference** for working on the project: it explains what
MAK is and why, walks through every subsystem in depth, and lays out exactly how to
set up, build, test, and submit changes.

By participating you agree to uphold the [Code of Conduct](CODE_OF_CONDUCT.md).

This file is long on purpose. MAK implements an unusual idea (a shared-memory concurrency
kernel for coding agents), and contributing effectively requires understanding the
architecture, not just the file layout. Read `Part I` for the mental model, `Part II`
when you need subsystem detail, and `Parts III–V` for the day-to-day workflow,
roadmap, and design rationale.

> [Current status](#current-status)

> [Open problems](#open-problems)

---

## Table of contents

- [Part I — Understanding MAK](#part-i--understanding-mak)
  - [What MAK is](#what-mak-is)
  - [Why not Git worktrees?](#why-not-git-worktrees)
  - [Architecture at a glance](#architecture-at-a-glance)
  - [End-to-end data flow](#end-to-end-data-flow)
  - [Current status](#current-status)
  - [Benchmark: MAK vs. git worktrees](#benchmark-mak-vs-git-worktrees)
- [Part II — The subsystems in depth](#part-ii--the-subsystems-in-depth)
  - [Core types, exceptions, logging](#1-core-types-exceptions-logging)
  - [Node Store](#2-node-store)
  - [The AST pipeline](#3-the-ast-pipeline)
  - [Lock Manager](#4-lock-manager)
  - [Conflict Detector](#5-conflict-detector)
  - [Scheduler](#6-scheduler)
  - [Agent Runner & Adapters](#7-agent-runner--adapters)
  - [Planner & human-in-the-loop review](#8-planner--human-in-the-loop-review)
  - [Git integration](#9-git-integration)
  - [Session lifecycle](#10-session-lifecycle)
  - [Configuration](#11-configuration)
  - [Command-line interface](#12-command-line-interface)
  - [Interactive CLI app](#122-interactive-cli-app-cli)
  - [Model catalog](#13-model-catalog)
  - [Local runtimes](#14-local-runtimes-maklocal)
- [Part III — Developing](#part-iii--developing)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Project layout](#project-layout)
  - [The quality gates](#the-quality-gates)
  - [Coding standards](#coding-standards)
  - [Commits, branches, and pull requests](#commits-branches-and-pull-requests)
- [Part IV — Where to contribute](#part-iv--where-to-contribute)
  - [Open problems](#open-problems)
  - [Good first contributions](#good-first-contributions)
  - [How MAK was built (history)](#how-mak-was-built-history)
- [Part V — Design decisions & rationale](#part-v--design-decisions--rationale)
- [Glossary](#glossary)
- [License](#license)

---

# Part I — Understanding MAK

## What MAK is

MAK is a **kernel for concurrent multi-agent software development**. The goal: let
several coding agents edit one shared codebase at the same time — without Git
worktrees, without merge conflicts, and without a reconciliation step at the end.

Most multi-agent coding systems give each agent its own Git branch and merge at the
end. That is a **message-passing** architecture: agents work in isolation and
synchronize only at boundaries, by which point the dependency information needed to
resolve conflicts has been lost.

MAK takes the **shared-memory** approach instead. All agents operate on the same
working directory. The kernel owns a symbol-level lock table and arbitrates
concurrent access the way an operating system arbitrates shared memory between
threads — with reader-writer locks, dependency tracking, and deadlock detection.
Git is demoted to a post-hoc audit log, written *after* MAK validates an agent's
output.

**Core constraint:** MAK is self-contained and bootstrap-capable. There is no
external orchestration system. The kernel manages everything — planning,
scheduling, lock arbitration, agent lifecycle, conflict detection, and file
reconstruction — in a single Python process.

## Why not Git worktrees?

Worktree-based systems defer conflict resolution to *merge time*, where the
dependency graph between changes is no longer explicit. MAK resolves conflicts at
*scheduling time*, where the dependency graph is known and locks can be
pre-allocated to prevent conflicting concurrent writes from ever happening.

Two corollaries shape the whole design:

- **The node store, not the filesystem, is the source of truth.** Files on disk are
  *derived artifacts*, reconstructed on demand from the committed node versions.
- **An agent never sees the whole file.** It receives only the AST nodes it holds
  write locks on (plus read-only context), edits them in isolation, and returns the
  modified fragments. The kernel reassembles the file. This is the shared-memory
  model: agents see a window into the codebase, not the codebase.

**Why is the LLM confined to the planner?** Every LLM call in the runtime path adds
latency and unpredictability. Task decomposition genuinely needs language
understanding; everything downstream — graph traversal, lock arbitration, AST
reconstruction, conflict detection — is deterministic and stays that way.

## Architecture at a glance

See [`diagram/`](diagram/README.md) for the component architecture and execution
sequence as editable Mermaid sources and PNG exports, including recovery,
transactional writes, and post-wave fix-ups. Rendering instructions and the
shared Mermaid style configuration live alongside the diagrams.

```
┌─────────────────────────────────────────────────────────────────────┐
│                            MAK KERNEL                               │
│                                                                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌───────────────────┐   │
│  │   Planner   │───▶│ Dependency Graph │───▶│    Scheduler      │   │
│  │  (LLM call) │    │    (DAG)         │    │  (DAG traversal)  │   │
│  └─────────────┘    └──────────────────┘    └────────┬──────────┘   │
│                                                      │              │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                      Lock Manager                             │  │
│  │   node_id → { holder, mode, acquired_at, timeout }            │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                      Node Store                               │  │
│  │   (file, kind, qualified_name) → versioned AST fragment       │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                   Conflict Detector                           │  │
│  │        parse gate → structural checks → accept/reject         │  │
│  └────────────────────────────────────────────────────┬──────────┘  │
│                                                       │             │
│  ┌────────────────────────────────────────────────────▼──────────┐  │
│  │                    Agent Runner                               │  │
│  │   route to adapter → assign task → collect TaskResult         │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
   API adapter:          API adapter:         API adapter:
   anthropic_api         openai_api           gemini_api
   (+ CLI fallbacks: claude_code / codex / copilot)
          │                    │                    │
          └────────────────────┼────────────────────┘
                               ▼
                    Shared working directory
                    + Node Store (on disk)
                    + Git (audit log only)
```

The kernel manages four moving parts:

- **Node Store** — decomposes the codebase into independently lockable AST nodes
  (functions, methods, classes, module headers); the source of truth.
- **Lock Manager** — a reader-writer lock per node; atomic, all-or-nothing
  acquisition; deadlock detection.
- **Scheduler** — turns the planner's subtask DAG into running work, pre-allocating
  locks before dispatch and unblocking downstream tasks as dependencies complete.
- **Agent Runner** — calls agents through a swappable adapter interface; API
  adapters (Anthropic/OpenAI/Gemini SDKs) are primary and return structured JSON.

## End-to-end data flow

```
User: "Implement topological sort in the scheduler module."
│
▼
Planner (one LLM call)
  → SubTask A: implement TopologicalSorter.sort   [write: dag.py::function::...sort]
  → SubTask B: implement Scheduler.tick           [write: scheduler.py::...tick]
                                                  [read:  dag.py::...sort]  (depends on A)
│
▼
DAG builder:  A ──▶ B   (B depends on A)
│
▼  (optional) Human-in-the-loop review of the plan: approve / edit / abort
│
▼
Scheduler tick #1
  A is ready → atomically acquire write lock on dag.py::...sort
  B waits for A
  → dispatch A to an agent (via its adapter)
│
▼
Agent Runner
  → enrich the TaskBundle with the current source of A's write targets + read context
  → send to the adapter; agent returns a TaskResult with the modified fragment(s)
│
▼
Collection phase
  → compile() each new fragment (enforces all Python compile-time rules)      ✓
  → conflict detector (compile() gate + structural checks)                   ✓
  → reconstruct the affected file from committed fragments + staged versions,
    compile()-validate the result *before* committing (transactional)
  → commit fragment versions, write the file, release A's locks
  → write a [MAK-A] audit commit
│
▼
Scheduler tick #2:  A complete → B unblocked → dispatch B  → (same collection phase)
│
▼
Session complete → run the test suite → push if green → write the session summary
```

## Current status

The **kernel is functionally complete and well-tested**: **2035 tests pass**
(plus three pre-existing, unrelated `TestIterSourceFiles` failures that predate
Wave 20 and are tracked, not fixed, by it — see Known limitations),
`mypy --strict mak cli` is clean, and `ruff check mak cli tests` is clean (the
gate was extended to `cli/` in Wave 17 — see below). The concurrent
shared-memory pipeline — the project's reason to exist — runs end-to-end and is
proven by an integration gate, and a real agent's rewritten source now reaches the
node store over the wire. Primary development is done; the work now is the **open
problems** in [Part IV](#part-iv--where-to-contribute) — evaluation, planner
efficiency, and multi-language support.

The module-by-module state:

| Module | Status |
|---|---|
| `mak/core/` (types, exceptions, logging) | Complete |
| `mak/config.py` + `mak/config.yaml` | Complete |
| `mak/node_store/` | Complete |
| `mak/lock_manager/` | Complete |
| `mak/agent_runner/` (runner, registry, protocol, API adapters) | Complete |
| `mak/scheduler/` | Complete |
| `mak/conflict_detector/` | Complete |
| `mak/planner/` (planner, review, LLM backends, response parsing) | Complete |
| `mak/git_integration/` | Complete |
| `mak/models/` (self-refreshing model catalog, Wave 14) | Complete |
| `mak/local/` (local runtime detection + native Ollama client, Wave 15) | Complete |
| `mak/session.py` | Complete (concurrent) |
| `mak/bootstrap.py` (composition root) | Complete |
| `mak/__main__.py` (CLI entry point) | Complete |
| CLI subprocess adapters (`claude_code`, `codex`, `copilot`) | Complete |
| `mak/agent_runner/sandbox.py` (Docker isolation) | Complete |
| **Concurrent execution** | **Complete (Wave 5)** — see below |
| **Pipeline integrity** | **Complete (Wave 11)** — no false conflicts, no store self-pollution, no silent drops |
| **Agent output budget / truncation safety** | **Complete (Wave 12)** — no truncated reply reads as success, no laundered no-op |
| **Dependency context** | **Complete (Wave 13)** — a task receives what its dependencies built; an empty bundle is refused, not dispatched |
| **Context budget** | **Complete (Wave 16)** — the caller layer is bounded and evidence-filtered; the cascade guard runs from both front ends |
| **Write-path safety & state durability** | **Complete (Wave 17)** — every entry point that turns a node id into a path is containment-checked; all three persisted state files are crash-safe; `mypy --strict`/`ruff` now cover `cli/` too |
| **Cost & disk bounds, log fidelity** | **Complete (Wave 18)** — a run has a token ceiling and the store a retention policy; a no-op cannot be asserted about code that never existed; enrichment and ingestion stop paying for what they discard |
| **Local LLM support** | **Complete (Wave 15)** — `local_api`/`ollama_api` transports, native context sizing, a parse→repair→retry loop, and an app mode (`cloud`/`local`/`hybrid`) that reaches the prompt with no API key at all |
| **State preservation & truthful outcomes** | **Complete (Wave 19)** — a real commit transaction with a journal and restart recovery; the store reconciles with the working tree at startup instead of discarding human edits; audit commits use a private Git index; a run reports its *aggregate* outcome and teardown reports what the suite actually did; one owner per project |
| `mak/semantic/` (read sets, stale reads, interface/registrar locking, contracts, cascade-on-the-graph, optional gates) | Complete (Wave 20) |
| **Semantic conflict detection** | **Complete (Wave 20)** — every shape in PLANS §5's taxonomy is prevented or detected against a real MAK run, with a git-worktree comparison recorded (§5.2) |
| `cli/` (interactive CLI app) | Complete |

> ### ⚠️ The mental model to hold before contributing
>
> **The node store, not the filesystem, is the source of truth, and an agent is a
> pure fragment transform** — one node's source in, one node's rewritten source out.
> An agent never roams the repo or edits disk directly; it returns the new source of
> each node it was granted (`TaskResult.new_sources`), and the *kernel* stages,
> validates, conflict-checks, commits, reconstructs, and writes. Anything an agent
> returns outside its lock grant is refused — and *said out loud*: refusals are
> logged with the id and the grant, and a symbol id returned under a whole-file
> grant is folded into it rather than discarded (§7.4). The agent receives its write-target
> sources plus an automatically-enriched context window: same-file sibling nodes,
> cross-file callers of the target symbols, and the committed output of the tasks it
> `depends_on` are included read-only, so the agent always arrives with the full
> dependency picture even when the planner did not enumerate it (see §3.2). A bundle
> that would carry *nothing* to a task that has dependencies is a kernel defect and
> is refused rather than dispatched. Internalize this and the rest of the codebase follows:
> the lock table, scheduler, conflict detector, and transactional commit all exist
> to make that fragment-transform contract safe under concurrency.
>
> The concurrency *is* done and proven. `Session.run` dispatches every
> lock-satisfiable ready task onto a bounded thread pool (`max_concurrent_agents`),
> **batches** concurrently-completing results into one multi-task conflict-detection
> round (so the cross-agent checks fire), commits in a deterministic order against the
> batch's already-committed peers, re-validates write-lock ownership at commit time,
> and renews in-flight leases with a heartbeat. Atomic lock pre-allocation makes the
> pipeline deadlock-free; a `DeadlockDetector` watchdog is defense in depth. The gate
> is `tests/test_concurrency_integration.py`. A real model can drive the whole thing
> given an API key (a live hosted-model call is simply not exercised in CI).

## Benchmark: MAK vs. git worktrees

[`benchmark/`](benchmark/) is a fair, reproducible head-to-head between MAK and the
git-worktree multi-agent model it was designed to replace. Both sides run the **same
workload** with the **same agents** (same models, same per-operation prompt, same
task assignment); the only thing that differs is the coordination model, so any
difference in the numbers is attributable to that.

### The workloads

The original two targets share a shape — a `toolkit` library of stubs plus a
shared dispatch table, `registry._register_all`, that **every** operation must add one
line to — at two sizes:

- **Basic** — **9 operations** across 3 modules (`strings`, `numbers`, `sequences`); a
  30-test oracle.
- **Template 2** — **90 operations** across 9 modules (`strkit`, `numkit`, `seqkit`,
  `dictkit`, `datekit`, `mathkit`, `parsekit`, `setkit`, `codekit`) — real-utility-style
  functions in the spirit of `boltons`/`more-itertools`/`toolz` (Levenshtein distance,
  Roman numerals both ways, calendar math, prime sieves, small parsers, set algebra,
  ciphers); a 270-test oracle. It is **generated** from `harness/template2_spec.py` by
  `tools/gen_template2.py`, so its stubs, reference implementations, and tests cannot
  drift — and a reference self-test (fill every stub from the spec, run pytest) proves the
  oracle is internally consistent before any model is called.

That shared `_register_all` is the whole point: it is the one symbol every agent must
touch. Under MAK a node-level write lock serializes those edits and none are lost;
under worktrees every branch edits it independently, so every merge after the first
collides there and must be reconciled. Module files are assigned one-agent-per-module
so they merge cleanly — the conflict is isolated to exactly the contended symbol. Pick a
target with `--project basic|2|3|4|all` (default `all`).

### Template 4: multi-tenant job service

`benchmark/project_template_4/` adds 24 function tasks across `tenancy`, `submission`,
`scheduling`, `leasing`, `lifecycle`, and `operations`. Its 152-test oracle comprises
112 contract/boundary checks, 30 wiring checks across `routes`, `events`, and
`policies`, and ten full service workflows. It covers tenant isolation, quotas,
JSON normalization, idempotency conflicts, retry/backpressure policy, stale leases,
worker recovery, cancellation, dead-letter replay, pagination, retention, and metrics.
`Job` and `Submission` are immutable dataclasses; timestamps are explicit inputs.

Implementation and maintenance:

- `benchmark/harness/template4_spec.py` owns public contracts, mock references, and
  explicitly specified expected results. `template4_workflows.py` owns independent
  integration scenarios. `benchmark/tools/gen_template4.py` regenerates the fixture;
  edit these sources rather than generated Python files.
- `benchmark/harness/planner.py` produces one validated plan using
  `anthropic:claude-opus-5` per repetition. It exposes only public contracts, model definitions, and wiring targets.
  The strict JSON plan assigns every module exactly once, uses every worker, and
  provides guidance. Literal line breaks in JSON strings are accepted; invalid
  control characters and incomplete JSON remain rejected. Validation failures get
  up to three attempts with corrective feedback, then stop before workers run.
  Anthropic planners use an 8,192-token response budget; workers retain 2,048.
  Every attempt contributes to planner time/token/call totals. `PlanAttempt` records
  raw responses, usage, and errors, written immediately to
  `.runs/4/planner-N/attempt-M.json` and retained in successful saved plans.
  `apply_plan` returns a new workload so guidance cannot accumulate between repeats.
- Three uniquely named `anthropic:claude-opus-5` workers are the Template 4 default.
  `--models` overrides workers and `--planner-model` overrides its planner. Other
  targets retain their previous defaults. `--agents` selects the default worker count;
  an explicit model list overrides the count. Template 4 permits one to six workers.
- Both runners receive identical ownership and guidance. They use the existing
  function-edit adapter and deterministic registrations. Template 4 tables construct
  local dictionaries; `registration_source`, `add_registration`, and the mock merge
  resolver preserve that scaffold while retaining the old tables' behavior.
- `RunResult` now records `planning_usage` and `planning_seconds`; `RunMeta` records
  `planner_model`. Totals include the shared planning cost once on each side, with
  separate subset rows in reports. Legacy JSON loads with zero planning cost.
  `.last_run.4.json` retains every plan and its actual usage, alongside aggregates and
  per-repeat samples. `--keep` retains `.runs/4/plan-N.json` and final working copies.
- Report ordering includes Template 4 after the three existing targets. Runs and
  `--render-only` update `benchmark/README.md` and `benchmark/STATS.md` with an exact
  `## Template 4` heading. Earlier saved project results remain included. The CLI
  rejects nonpositive repeats and empty model lists instead of silently adjusting them.

From the repository root, with the development dependencies installed:

```bash
python benchmark/tools/gen_template4.py
python -m pytest tests/test_benchmark_template4.py tests/test_benchmark_traditional.py -q
python benchmark/run_benchmark.py --mode mock --project 4 --keep
# Requires ANTHROPIC_API_KEY; one Opus 5 planner plus three Opus 5 workers:
python benchmark/run_benchmark.py --mode real --project 4 --repeat 10
```

Regression tests verify deterministic regeneration, full baseline collection with
all checks failing, all four workloads passing through both mock runners, malformed
planner rejection, reference isolation, real-planner call accounting through a test
double, identical ownership/guidance, legacy report loading, saved plans, and the
repeated-run CLI/report flow. The checked-in Template 4 statistics are explicitly
labelled mock results; no paid model calls were used to validate this change.

Initial validation on Python 3.13: all 26 benchmark tests passed, and the full
repository suite had 1,694 passes with three existing `src/**` ingestion/glob
failures. Those same three failures were reproduced from an unmodified `HEAD`
archive; they are outside the benchmark changes.

The planner-response fix adds seven regression cases for literal line breaks,
truncation, invalid controls, retry exhaustion, diagnostic persistence, accumulated
usage, and planner/worker token-budget separation. All 33 benchmark tests pass.

**Interpretation limits:** this is a controlled coordination benchmark with predefined
function tasks, not MAK's production planner or an autonomous repository migration.
Workflows test the final combined service; worker tasks do not introduce execution
dependencies. Pure service logic does not model database durability or network races.
The traditional baseline invokes worker calls sequentially but reports simulated
parallel call time plus measured merge time; MAK uses measured execution time.
Setup/tests are excluded, planner cost is included, and stochastic implementations
can differ even with identical prompts. Mock timing says nothing about Opus speed.

### Fairness controls

- **Same agents/models** on both sides, and the **same agent layer** — identical
  prompts, and the registry line itself is applied by a deterministic helper, so the
  model's only creative job is the function body. The comparison isolates
  *coordination*, not registry-editing skill.
- **Same assignment** (operation → agent) and the **same per-workload test oracle**, run
  the same way.
- **Parallel timing model.** The worktree side's agents work concurrently, so its
  implementation phase is charged as `max` over agents of that agent's call time (not
  the sum); the sequential merge+resolve phase is added on top. MAK is charged its real
  end-to-end wall-clock. If anything this is generous to the worktree side.
- The worktree baseline **resolves** each conflict with one model call (rather than
  leaving conflict markers, which would fail import and collapse accuracy) — the fairer,
  stronger baseline.
- **A malformed agent response is isolated, not fatal — on both sides.** A garbled
  output (e.g. an unparseable function) is rejected symmetrically: MAK's commit phase
  drops a node whose staged source fails its parse gate and retries the task, and the
  worktree runner refuses to splice unparseable Python into the module. One bad call
  therefore costs *that operation* its tests rather than crashing the run — and the token
  cost of the call still counts. (This fired on the Template 2 runs; see below.)
- **A per-test timeout guards the oracle.** A real agent can implement an algorithmic
  function with an infinite loop (a wrong `while` in `collatz_steps`, `nth_prime`, …).
  The template's `conftest.py` installs a SIGALRM-based per-test timeout, so a runaway
  implementation fails *that* test instead of hanging `pytest` forever — applied to both
  the MAK and worktree measurements, so it favours neither side.

### Real results

Recorded runs: **3 × `claude-sonnet-4-6`** (the same three agents on both sides).
The **Template 2** numbers are the **mean of 10 independent runs** (the per-run breakdown
is in [`benchmark/STATS.md`](benchmark/STATS.md)); Basic is a single representative run.

**Basic — 9 operations, 30 tests**

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 20.37s | **11.64s** |
| Total tokens | **2,052** | 3,192 |
| — input / output | 1,229 / 823 | 2,153 / 1,039 |
| Model calls | 9 | 11 |
| Accuracy (tests passed) | 30/30 (100%) | 30/30 (100%) |
| Registry merge conflicts | **0** | 2 |
| Conflict-resolution calls | **0** | 2 |

**Template 2 — 90 operations, 270 tests** (mean of 10 runs)

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 226.54s | **99.52s** |
| Total tokens | **18,339** | 23,760 |
| — input / output | 10,378 / 7,961 | 13,481 / 10,279 |
| Model calls | 90.6 | 92 |
| Accuracy (tests passed) | **253.1/270 (94%)** | 251.6/270 (93%) |
| Registry merge conflicts | **0** | 2 |
| Conflict-resolution calls | **0** | 2 |

Across the 10 runs MAK's accuracy was rock-steady (253/270 in nine runs, 254 in one;
σ ≈ 0.3 tests) while the worktree side ranged 247–254 (mean 251.6) — MAK matched or beat
it in **every** run, and never fewer tokens or more than zero conflicts.

### Reading it carefully

**Basic — the structural signal.**

- **Tokens — MAK wins by 36%.** Both sides make the same 9 implementation calls; the
  entire ≈1,140-token gap is the worktree side's **two conflict-resolution calls** (the
  registry collided on 2 of the 3 merges), which re-send the conflicted file as input.
  MAK reconciles nothing, so it never makes those calls. This is the cleanest, most
  robust signal in the benchmark.
- **Merge conflicts — 0 vs 2, by construction.** MAK serializes the registry node under
  one write lock; each task reads the latest committed version and appends, so a
  collision is *impossible*. The worktree side hits `agents − 1` conflicts (the first
  branch merges clean; each later branch collides on `_register_all`).
- **Accuracy — tied at 100%, but read the asterisk.** The functions are small, the model
  implements them correctly, and — critically — the resolver merged the 2 conflicts
  *correctly this time*. The failure mode MAK removes is a resolver that drops or
  garbles a `register(...)` line: that operation silently never enters the table and its
  dispatch test fails. The tie reflects easy tasks plus a strong resolver, not the
  absence of a difference — which is exactly what the heavier target exposes.
- **Time — the worktree side was faster, and the *why* matters.** This workload is
  **maximally contended**: all 9 tasks must edit the single shared node. MAK's
  correctness on that node comes from serializing its writes, so the 9 tasks run
  effectively sequentially (≈20s). The worktree model lets all three agents implement
  fully in parallel and defers the collision to a cheap merge phase — it wins wall-clock
  *precisely because it does not coordinate during implementation*, and pays for it
  afterward in tokens, conflicts, and the risk of lost work.

**Template 2 — what changes at 10× the size (90 operations), averaged over 10 runs.**

- **Tokens — MAK wins by 23%** (18,339 vs 23,760, mean of 10). Both sides make ~90
  implementation calls of comparable size; the gap is the worktree side's heavier input (it
  re-sends the conflicted registry on its two resolution calls) plus those extra calls
  themselves. MAK reconciles nothing, so it never pays that — and the absolute saving
  (≈5,400 tokens) is far larger than on the small target even though the *percentage* is
  between Basic's two numbers. The token advantage was the most robust signal: MAK spent
  fewer tokens in **all 10** runs, tightly clustered (≈18.1k–18.7k vs ≈23.7k–23.9k).
- **Accuracy — MAK ahead, 94% vs 93%** (253.1/270 vs 251.6/270, mean of 10). At this size
  the models get a handful of the harder algorithms wrong on *both* sides — that is real,
  expected LLM noise and exactly why the suite has a per-test timeout and a parse gate. The
  point is the *delta* and its stability: MAK landed 253/270 in nine of ten runs (254 once),
  while the worktree side swung from 247 to 254 and averaged lower. The worktree side loses
  everything MAK loses **plus** the occasional function whose malformed output the merge
  keeps as a stub (5 such agent-output notes across the 10 runs) **plus** the structural
  exposure of two registry conflicts every run. MAK matched or beat it in every run.
- **Conflicts — invariantly 0 vs 2, by construction.** Every one of the 10 worktree runs
  hit exactly two `_register_all` collisions (three branches, the first merges clean, the
  next two collide); MAK serializes the node and hits zero. This is `agents − 1` for the
  worktree model regardless of project size — not noise, structure.
- **MAK averaged 90.6 calls for 90 operations** — essentially one per task. In five of the
  ten runs a task lost the commit race and the kernel re-ran it (its bounded
  re-validate-and-retry path, capped by `max_attempts`), adding one or two calls; the other
  runs were exactly 90. The worktree side made 92 every run (90 + 2 resolutions).
- **Time — still ~2.3× slower (226.5s vs 99.5s), same reason as Basic, amplified.** 90
  tasks all contend on the one registry node, so MAK serializes their commits while the
  three worktrees implement fully in parallel and pay only a cheap merge. The wall-clock
  trade is unchanged at scale; the benchmark remains the worst case for MAK's latency and
  a strong case for its tokens and correctness.

### Caveats

- **Single model.** Both recorded runs used three Claude agents because the OpenAI
  account had no active billing and the Gemini key's prepaid credits were depleted. Same
  model on both sides keeps it fair, but it is not a cross-model comparison. Supply your
  own keys and `--models` to compare across providers.
- **Maximally contended.** Every task touches the shared node on both targets. Real
  projects are mostly independent work plus some contention; a larger, *partially*-
  contended workload would show MAK's parallelism on the independent part and widen the
  token/correctness gap on the contended part. Extending the benchmark in those directions
  is an [open problem](#also-extend-the-benchmark).

### Semantic conflict corpus (Wave 20)

A second, separate benchmark: not tokens/time/accuracy on a shared workload, but
whether each coordination model **catches** a semantic conflict at all.
[`benchmark/semantic/`](benchmark/semantic/) seeds one scenario per shape in
PLANS §5's taxonomy — two scripted edits, an oracle that only fails on the
uncoordinated combination — and runs each through a real MAK session and
through a worktree-shaped merge of the same two edits. Full mechanism,
scripting details, and the false-positive/extra-call guarantees are in
[§5.2](#52-semantic-conflicts-wave-20); the result:

| shape | scenario | MAK | worktrees |
|---|---|---|---|
| 1 | stale read (write skew) | detected (commit) | missed |
| 2 | signature change vs new call | detected (wave end) | missed |
| 3 | behaviour change, same signature | missed / with `impact_tests`: detected | detected (CI tests) |
| 4 | deletion / rename | detected (wave end) | missed |
| 5 | override against a changed base | detected (wave end) | missed |
| 6 | duplicate registration key | detected (commit) | detected (textual conflict) |
| 7 | order-dependent table | **prevented** | detected (textual conflict) |
| 8 | new required field vs new construction | detected (wave end) | missed |
| 9 | duplicate implementation | detected (wave end) | missed |
| 10 | out-of-store artifacts | not representable | not representable |

```bash
python benchmark/semantic/run_semantic.py            # markdown table
python benchmark/semantic/run_semantic.py --json      # one JSON object per shape
```

### Running it

```bash
python benchmark/run_benchmark.py --mode mock                     # keyless self-test (both targets)
python benchmark/run_benchmark.py --mode real                     # real models (needs keys), both targets
python benchmark/run_benchmark.py --mode real --project 2         # just the heavy 90-op target
python benchmark/run_benchmark.py --mode real --project 2 --repeat 10  # mean of 10 runs (as published)
```

`--repeat N` runs the target N times and reports the **mean**, plus a per-run breakdown
table in `STATS.md` so the average is auditable; the published Template 2 numbers are
`--repeat 10`. A per-call liveness line is printed to stderr (`[call N] implement … (in= out=)`)
so a long multi-run sweep is visibly progressing and never silently stuck.

Results are written to `benchmark/README.md` (summary) and `benchmark/STATS.md`
(detail), one labelled section per target; `--render-only` regenerates them from the last
runs without spending tokens. See [`benchmark/README.md`](benchmark/README.md) for the
full guide.

---

# Part II — The subsystems in depth

This part is the technical design reference. Each section is independently readable;
skip to the subsystem you're touching.

## 1. Core types, exceptions, logging

`mak/core/` holds the contracts every other module imports.

- **`types.py`** — the shared value objects, all frozen dataclasses where possible:
  - `NodeId` — a `NewType(str)`. The identity of a lockable code unit (see the
    [node identity scheme](#node-identity)).
  - `NodeFragment` — a node's raw source plus metadata (`node_id`, `kind`,
    `source`, `version`).
  - `LockMode` — `READ`, `WRITE`, `INTENT_WRITE` (a `StrEnum`).
  - `LockEntry` — a single held lock (`node_id`, `mode`, `holder`, `task_id`,
    `acquired_at`, `timeout_s`).
  - `ResourceRef` / `ResourceKind` — a reference to a file- or symbol-level resource.
  - `TaskBundle` — the unit sent *to* an agent: `task_id`, `description`,
    `target_nodes`, a `context` dict (enriched with write/read source), and
    (Wave 12) `retry_note: str | None` — feedback attached to a re-dispatch
    explaining why the previous attempt produced nothing usable, so the second
    attempt is not a byte-identical re-issue of the first.
  - `TaskResult` — the unit returned *from* an agent: `task_id`, `success`,
    `modified_nodes`, `new_sources`, `error`. Wave 12 adds four fields that make
    a truncated reply distinguishable from a deliberate no-op:
    `no_changes_required` (the agent's *positive assertion* that it inspected
    the targets and found nothing to change — required for the no-op path
    below, §10), `stop_reason` / `usage` (the provider's own stop signal and
    token counts, carried through for every attempt, good or bad), and
    `retryable` (False for a failure that repeats verbatim on an identical
    request — a refusal — so the session doesn't burn its whole attempt budget
    re-asking it). `error_kind` (`truncated` / `refused` / `protocol` / `api`)
    carries *which* failure it was, from the exception class: `retryable` says
    another attempt is worth making, not what to change, and a retry that cannot
    differ from the attempt that failed spends the budget re-earning the same
    answer (§10).
  - `SubTask` — a planned unit of work: `task_id`, `description`, `target_nodes`
    (what it will *write*), `context_nodes` (what it needs to *read*),
    `depends_on`, `agent_type`.
- **`exceptions.py`** — every domain exception derives from `MakError`:
  `LockError`, `SchedulingError`, `ConflictDetectionError`, `GitIntegrationError`,
  `NodeStoreError`, `PlannerFailedError`, `PlanReviewAborted`, `SessionError`,
  `AgentError`, `UnknownAgentTypeError`, `ConfigError`. Wave 12 adds
  `AgentResponseError(AgentError)` and three subclasses that give a rejected
  provider response a name instead of a bare string: `AgentTruncatedError` (hit
  the output cap mid-generation — retryable), `AgentRefusedError` (the model
  declined — **not** retryable, since the same prompt earns the same refusal),
  and `AgentProtocolError` (the HTTP call succeeded but the body could not be
  decoded into a `TaskResult` — a decode failure, never a transport one). All
  three carry `stop_reason` and `usage` so the runner can put them on the
  failed `TaskResult` it returns. Wave 17 adds `UnsafeNodeIdError(MakError)` —
  raised when a node id's file component would resolve outside the tree it may
  write to (`mak/core/paths.py`, §2).
- **`logging.py`** — `SessionLogger`: an append-only JSON-Lines event log. `EventType`
  is a `StrEnum`; `LogEntry` round-trips via `to_json()` / `from_json()`. Writes are
  serialized under a lock and flushed, so events never interleave or truncate.
  Two of the event types exist purely so a failed run can be diagnosed *without
  re-running it*: `AGENT_RESULT` (what an agent actually returned, per attempt —
  Wave 12 adds `stop_reason`, `usage`, and `no_changes_required` to this payload,
  which is the whole reason a truncation is provable from the log) and
  `SOURCE_DROPPED` (anything MAK refused to stage, with the id and the grant).
  Wave 12 adds `ACCEPTED_NOOP` — a task that closed because the agent *asserted*
  no change was needed, logged distinctly from an ordinary `TASK_COMPLETED` so
  the log shows "decided there was none" apart from "did the work". Wave 13 adds
  `TASK_DISPATCHED` for the other direction — what the kernel *gave* the agent
  (context entry counts and bytes per attempt, plus `starved`), because a bundle's
  context is everything an agent knows about the codebase and MAK used to dispatch
  an empty one without recording it anywhere (§3.2). Wave 16 adds the per-layer
  `layers` breakdown to that payload, so the log answers *which* layer bought the
  tokens rather than only how many there were. Wave 18 adds `TASK_FAILED` and
  `AGENT_REMAPPED`, both of which were previously logged as `TASK_COMPLETED` —
  the first with a `failed=True` flag a reader had to know to check, the second
  on a task that had not started, let alone completed. Anything counting
  completions by event type, a human skimming the log included, over-reported.
  The rule the two encode: **an event names the thing that happened**, and a flag
  inside a payload is not a substitute for the right name.

## 2. Node Store

The Node Store (`mak/node_store/`) is MAK's equivalent of shared memory. It replaces
the filesystem as the source of truth for code.

### Node identity

A **node** is the smallest independently lockable unit of code. Identity is
**position-independent** — based on qualified name, not line number — so inserting a
new function does not invalidate another agent's lock on an existing one. The id
format is:

```
<file_path>::<kind>::<qualified_name>
```

| Kind | Example id |
|---|---|
| `function` (top-level def) | `mak/scheduler/dag.py::function::topological_order` |
| `class` (the class *shell*) | `mak/lock_manager/rwlock.py::class::RWLock` |
| `method` (def inside a class) | `mak/lock_manager/rwlock.py::method::RWLock.acquire` |
| `module_header` (imports + leading constants) | `mak/config.py::module_header::__header__` |
| `module_body` (top-level code after the first def/class) | `mak/config.py::module_body::__body__` |
| `class_body` (class-level statements after a method) | `…::class_body::RWLock` |

Duplicate names (e.g. `@overload` stubs, conditional defs) are disambiguated with a
`#n` suffix so no symbol is silently dropped.

**Whole-file nodes (new-file creation *and* whole-file rewrites).** A node id may also
be a **bare file path** with no `::kind::name` suffix — e.g. `app/main.py`. This is a
*whole-file node*: the agent returns the entire file as one node, and reconstruction
writes it verbatim (a single fragment already *is* the file). It is how MAK creates a
brand-new file from an empty target: `list_nodes(path)`/`get_committed_fragments(path)`
return the exact-match bare node alongside any `path::…` fragments, locking uses the
bare path as its key (so commit-time re-validation lines up), and `reconstruct_file`
`mkdir -p`s the parent. A planner targeting `editor/main.py` (greenfield) therefore
works end to end.

A whole-file node may also target an **existing** file that was ingested as fragments
(e.g. an "audit / rewrite this whole module" task). Committing it **supersedes** that
file's fragments: `commit_node` drops every committed/pending `path::…` node
(`_supersede_fragments`) so the file is now defined by the one whole-file node alone.

This superseding is enforced at two additional levels for robustness:

- **`list_nodes(file_path)`** — when a bare whole-file node exists for a file, only
  that node is returned; stale `path::kind::name` fragment nodes are excluded. This
  prevents a scenario where a correctly-written whole-file node from a prior run
  coexists with re-ingested stale fragments: without this guard, reconstruction would
  concatenate whole-file content *and* every fragment, emitting every symbol twice.
- **`parse_file_into_nodes(file_path, …)`** — a thin wrapper over `sync_file` since
  Wave 19. When a whole-file node is already committed for the file, the node keeps
  its authority and is **not** re-fragmented (fragmenting it again would add the
  stale siblings that contaminate reconstruction) — but the supplied `source` is no
  longer *ignored*. It used to be: the method returned `[whole_file_nid]` without
  looking at what it was handed, which is how a human's edit to a whole-file node
  was silently reverted on the next run. A differing source now becomes that node's
  next version. See §10 for the reconciliation this is part of.
- **`list_nodes()` (no file filter)** — fragment nodes for files that have a
  whole-file node are omitted from the full inventory, so the planner never offers
  them as write targets.

(An existing file you do *not* whole-file-target stays decomposed into the qualified
fragments above; mixing fragment-level and whole-file edits to the *same* file in one
plan is rejected at plan time — see §8.)

### On-disk layout

Runtime state lives under `.mak/` (gitignored):

```
.mak/
├── node_store/
│   └── <mirrored source tree>/<file>.py/
│       ├── __header__.v1.py
│       ├── <Class>.v1.py
│       ├── <Class.method>.v1.py
│       └── metadata.json     ← index: kind, order, current version per node
├── lock_table.json           ← persisted lock state (rebuilt on crash recovery)
├── task_graph.json           ← DAG execution state (for crash recovery)
└── session.log               ← append-only event log
```

### `NodeStore` API

`NodeStore` (`store.py`) owns versioning and persistence. Key methods:
`get_node`, `put_node`, `commit_node`, `rollback_node`, `revert_node`, `get_staged`,
`list_nodes`, `get_committed_fragments`, `get_preview_fragments`,
`parse_file_into_nodes`, and — Wave 19 — `transaction`, `uncommit_node`,
`retire_node`, `sync_file`, `record_materialized` / `materialized_digest`.

Two of them are **maintenance only**, added in Wave 11 for the startup prune and
used by nothing on the edit path: `list_all_nodes()` returns every committed id
including fragments a whole-file node superseded (which `list_nodes` deliberately
hides), and `remove_node(node_id)` deletes a node outright — committed and pending
state, metadata, and its on-disk version directory (only when that directory really
resolves inside the store root). A rejected *edit* is still rolled back or reverted;
it is never removed.

**Three different "undo"s, and why each exists (Wave 19).** They are not
interchangeable, and conflating them is what produced two of the audit's P1s:

| Method | Undoes | Keeps history? |
|---|---|---|
| `rollback_node` | a *pending* (staged, uncommitted) fragment | n/a — nothing was committed |
| `revert_node` | one committed version, to `version - 1` | yes |
| `uncommit_node` | a **first**-version commit, back to *absent* | yes (the file stays on disk) |
| `retire_node` | a symbol the working tree no longer has | **yes** — the point of it |
| `remove_node` | everything, permanently (maintenance only) | no |

`revert_node` has no answer for a node whose committed version is 1: there is no
version 0, and its correct prior state is not an older version but no node at all.
That gap is why a failed first-version commit used to stay in the store —
`uncommit_node` is the piece it could not express, and `transaction()` is what
composes the two into a whole-commit rollback.

`retire_node` is the **deletion policy**, and deliberately not `remove_node`. Using
the hard delete to record "a human deleted this function" would destroy exactly the
history the store exists to keep. A retired node leaves the live set — no listing,
no reconstruction, no planner inventory — while its metadata entry (flagged
`retired`) and its on-disk versions stay, so `get_node(nid, version=n)` still
answers and `gc`'s forward-mapping orphan sweep still protects its directory.
`_load_from_disk` skips retired ids, so a deleted symbol does not resurrect on the
next run.

`get_preview_fragments(file_path, staged_overrides)` is used by
`_preview_is_valid` / `_assemble_preview` to build the prospective file *before*
committing — it substitutes staged versions for their committed counterparts and
re-applies each fragment's `indent_prefix` so that class methods appear at the
correct column (dedented method source concatenated directly would always fail the
`compile()` gate for files containing class methods).

**The preview must model what the commit would produce.** A *staged* whole-file
node supersedes the file's fragments here, exactly as `commit_node` does via
`_supersede_fragments`. Without that mirroring, a whole-file rewrite of a file
still stored as fragments previewed as the old fragments **plus** the entire new
file appended after them. That doubles every symbol, and — because this codebase's
modules open with `from __future__ import annotations` — puts a `__future__`
import mid-file, which `compile()` rejects outright. The gate then failed the task
with "reconstruction would produce invalid Python" on *every* attempt, discarding
~40 KB of correct agent output each time, for a state the commit would never have
built. Note that the greenfield case (no committed fragments) and the
already-whole-file case both worked; only a whole-file grant over a
fragment-stored file hit it, which is why it survived the existing
`test_whole_file_rewrite_of_existing_file_is_not_doubled` — that test's rewrite
happens to compile even when doubled, so the gate let it through and the commit
cleaned up afterwards.

The store **owns version assignment**: `put_node` ignores any version on the
incoming fragment and stamps it `current_committed + 1`, so callers never guess the
next version. Prior versions are retained on disk, which is what makes
`revert_node` (roll a committed node back to its previous version) possible.
Fragment order is preserved as `order` metadata so reconstruction emits source in
its original order. **All mutations are guarded by a re-entrant lock.**

**`transaction()` — the commit point (Wave 19).** `commit_node` used to be its own
commit point: it mutated the index, deleted superseded fragment directories, pruned
old versions, and *then* saved the metadata — so a failure anywhere after the first
of those left the store's representations disagreeing, with the files a rollback
needed already deleted. Inside a transaction those three destructive effects are
**deferred** and `_save_metadata` runs exactly once, at the end: **that save is the
commit point.** Before it nothing durable has changed and the in-memory index is
restored verbatim (including the version files `put_node` wrote while it was open);
after it the deferred deletions drain.

Pruning in particular *must* be deferred — it deletes the very version files a
rollback restores the index to. The transaction is re-entrant by depth, because the
operations that need it nest (`sync_file` opens one and calls `commit_node`, which
opens another); only the outermost block commits or rolls back. Outside a
transaction, `commit_node` still restores the entries it changed if the metadata
save raises, so even a bare commit cannot leave memory ahead of disk.

**`file_state.json`** is a sidecar recording the SHA-256 of the content MAK last
*materialized* for each file. It is what makes "did a human edit this?" answerable:
the store's own fragments cannot distinguish the edit MAK made from an edit someone
made afterwards. An unreadable sidecar reads as "never seen", which makes
reconciliation treat the working tree as authoritative — the safe direction to be
wrong in.

**Retention, ordering, and the generation counter (Wave 18).** Three changes to
the above, all of them about a store that has to survive months of use rather than
one run:

- *Retention.* "Prior versions are retained" used to mean **all** of them —
  every commit wrote a `v{n}.py` and nothing ever removed one, so
  `.mak/node_store/` grew monotonically for the life of a project. A commit now
  prunes back to `version_retention` versions of the node it committed (default
  5; the floor is **2** because `revert_node` needs one prior version to roll
  back to; `-1` restores the old unbounded behaviour). `_supersede_fragments`
  also deletes the superseded fragments' on-disk directories, which it never did:
  it dropped them from `_nodes`/`_pending`/`_metadata` and left directories on
  disk that no id in the store could ever address again. `NodeStore.gc()` applies
  both policies to a whole store — that is what `mak gc` calls — and additionally
  removes orphan directories by *forward*-mapping every live id to its directory
  and deleting what is left over. Never by parsing a path back into an id: `::`
  becomes `/` on disk, so `a/b.py` and `a/b.py::function::f` nest inside one
  another and the reverse mapping is ambiguous.
- *Ordering.* `order` is assigned `0..n` **per file**, so a global sort on
  `order` alone grouped every file's node 0 together, then every file's node 1.
  Harmless for reconstruction, which filters by file first — but the planner's
  inventory prompt was presented in an order no file actually has, which is
  exactly the input Wave 7 wants to make cheaper. Listings now sort by
  `(file_path, order)`.
- *Memoization + `generation`.* The sort is O(n log n) over the whole store and
  `list_nodes()` is called once per enrichment layer per dispatch **and** per
  retry. The ordering is memoized behind the existing `RLock` and invalidated
  whenever the *committed* set changes — staging and rollback deliberately do
  not invalidate, because they leave `_nodes` alone. The same invalidation is
  published as `NodeStore.generation`, a monotonic counter that lets a caller
  cache state derived from the store without having to learn what changed;
  `Session`'s cross-file symbol index is the first such caller (§10).

**Containment, and crash-safe persistence (Wave 17).** A node id's file component
becomes a real filesystem path in two places — `_fragment_dir` here, and
`Session._reconstruct_affected` on the work-dir side (§10) — and neither used to
check where it landed. `Path(root) / "/etc/x.py"` is `/etc/x.py`: an absolute
component discards everything before it, and a `..` component walks out of any
root. `_fragment_dir` is the store's single choke point for every fragment read,
write, and delete, so `mak/core/paths.py::check_node_id` is asserted there —
containment only (`mak_dir_name=None`), not the "is this project source?" question,
because the Wave 11 prune has to be able to **address** the `.mak/…` nodes an older
MAK ingested in order to delete them; a store that refused to name them could never
clean them up. `safe_path_under` additionally *resolves* the path, catching what a
string check cannot — a symlinked directory inside the tree pointing outside it.

Separately, `metadata.json` — rewritten on every commit — now writes through
`mak/core/atomic.py::write_text_atomic` (temp file in the same directory, `fsync`,
`os.replace`), so a kill mid-write leaves either the whole old file or the whole
new one, never a truncation. A metadata file that still can't be read (an older
truncation, or corruption from any other cause) is quarantined to
`metadata.json.corrupt` and the store starts with an empty index rather than
raising out of its own constructor — the fragments on disk are the valuable part
and are left untouched; only the index that pointed at them is rebuilt from
nothing. `lock_table.json` and `task_graph.json` get the same atomic-write
treatment and their own corrupt-read policies; see §4 and §10.

## 3. The AST pipeline

This is the kernel's core mechanism — it replaces Git's diff/merge with a
structured operation. Four phases:

### 3.1 Ingestion (file → fragments) — `ingestion.py`

> **Key design decision:** ingestion uses **raw-source span tiling**, *not*
> `ast.unparse()` and *not* `libcst`. The file is partitioned into line spans that
> tile it completely in source order; each fragment keeps its **raw source text**.
> Because nothing is ever re-rendered through an unparser or a CST, comments,
> decorators, blank lines, and formatting survive a round trip *by construction*.
> `libcst` is **not** a dependency.

Mechanics:
- `ast.parse` the file for structure, then tile by line spans:
  - leading import/constant block → `module_header`;
  - each top-level `def`/`async def` → a `function` fragment (decorator lines
    included — spans start at `min(decorator lineno)`);
  - top-level executable code between defs → `module_body` fragments.
- Classes decompose **one level**: a `class` *shell* fragment (the `class` line,
  docstring, and leading attributes) plus one `method` fragment per method, plus
  `class_body` fragments for class-level statements that follow a method. This is
  what gives **method-level lock granularity**.
- `parse_file_into_fragments(path, source=None)` returns fragments in source order;
  `walk_and_parse(root, include, exclude)` runs it over a directory tree.

**The walk prunes before it descends (Wave 18).** Both entry points into a tree —
`walk_and_parse` and `Session._ingest_work_dir` — used to `glob("**/*.py")` the
whole thing and *then* drop the excluded paths. The exclusion was correct; the walk
was not. It descended into `.venv`, `node_modules`, `site-packages` and
`__pycache__`, enumerating tens of thousands of paths it discarded immediately, and
on a repo with a populated virtualenv that was the slowest part of `initialize()`.
`iter_source_files(root, include, exclude, skip=…)` replaces both: an excluded
*directory* is skipped before it is entered, everything else is preserved. On this
repo — 217 ingested files, a populated `.venv` — the walk went from **496 ms to
12 ms** for a byte-identical file list.

"Identical" is load-bearing here, because a divergence means a file silently missing
from the node store and nothing downstream can detect that. Two details make it so.
The include patterns are matched by a small glob→regex translator
(`_include_regex`/`_segment_regex`) rather than `fnmatch.translate`, whose `*`
becomes `.*` and happily spans `/` — `src/*.py` would match `src/deep/x.py`; `**/`
compiles to "zero or more whole segments", which is what makes `**/*.py` match a
root-level file as well as a nested one, and a *trailing* bare `**` compiles to a
never-matching pattern because `Path.glob` resolves that to directories only. And
symlinked directories are not descended, matching `glob`'s `**`, which also protects
the walk from a symlink cycle. A directory is pruned only by a pattern ending in
`/**` — the shape that excludes a whole subtree — so a pruned directory is always
one whose every descendant the per-file check would have rejected anyway. The tests
are differential against the old glob-then-filter across nine pattern shapes and
three exclusion sets, plus the repo itself.

### 3.1.1 `.makignore` — the project's own ignore list — `makignore.py`

`node_store.exclude_patterns` is MAK's config-level default list. `.makignore` is
the project's own list: a gitignore-style file at the work-dir root that the user
owns and edits, read on every `Session.initialize()`.

**It is created automatically.** If the work dir has no `.makignore`, the first
session writes one with MAK's own state and `.git` already listed:

```gitignore
# .makignore — paths MAK never ingests into its node store.
.mak/
.git/
```

MAK never overwrites an existing file, including an empty one. Until it is written,
the same defaults apply in memory. It is written *after* the `git.require_clean_tree`
check, so a brand-new untracked file cannot fail that check on the run that creates
it; projects using that setting should commit `.makignore` afterwards.

**Syntax** is gitignore's, restricted to one file at the root:

| Pattern | Meaning |
|---|---|
| `# text` / blank line | ignored; `\#` for a literal leading `#` |
| `name` | no `/` → matches a file or directory named `name` at **any depth** |
| `dir/` | trailing `/` → matches **directories only** (and so everything under them) |
| `/top.py`, `pkg/mod.py` | a `/` at the start or middle → **anchored** to the work-dir root |
| `*`, `?`, `[a-z]` | wildcards that never cross `/` |
| `**/x`, `a/**/b`, `a/**` | `**` spans zero or more whole path segments; `a/**` is everything *inside* `a` |
| `!pattern` | re-includes what an earlier pattern ignored; `\!` for a literal `!` |

The **last** matching pattern wins. As in git, a file cannot be re-included when a
parent directory is ignored: with `gen/` followed by `!gen/keep.py`, `gen/keep.py`
stays ignored (use `gen/*` + `!gen/keep.py` instead).

**Where it applies.**

- **Walk.** `iter_source_files(..., ignore=…)` takes a `(rel_path, is_dir) -> bool`
  check. An ignored directory is pruned before it is entered, exactly like an
  excluded one, so a large ignored tree costs nothing. The session passes
  `MakIgnore.matches`, which checks a path on its own, because its parents were
  already checked on the way down.
- **Prune.** `prune_excluded_nodes()` also removes stored nodes whose file is now
  ignored, using `MakIgnore.is_ignored`, which checks every parent directory too.
  Adding a path to `.makignore` therefore removes it from the store on the next run;
  the file on disk is untouched.

**It is not the safety net.** The session's unconditional skip of its own `mak_dir`
(`_is_store_path`, §10) and the default `exclude_patterns` both stay. Deleting
`.mak/` from `.makignore`, or emptying the file, cannot reintroduce the
self-ingestion loop (`.mak/node_store/.mak/node_store/…`) that motivated Wave 11.
Tests: `tests/node_store/test_makignore.py` and `TestMakIgnore` in
`tests/test_session.py`.

### 3.2 Fragment dispatch (node store → agent)

When a task is dispatched, the session builds a `TaskBundle` and **enriches** it with
context from five layers, in order:

1. **Write targets** — the current committed source (`write_source:<id>`) for every
   node the agent will write.
2. **Planner-specified context nodes** — read-only source (`read_source:<id>`) for
   every `context_node` the planner included.
3. **Same-file siblings** — all other committed nodes in the same file(s) as the
   write targets, automatically included as read context — regardless of whether the
   planner listed them.
4. **Cross-file callers** — nodes elsewhere in the repo whose stored source contains
   a word-boundary match for a write-target symbol name (the session scans all stored
   nodes with one regex). This ensures an agent editing `def apple` also receives
   context from `def dog` in a different file that calls `apple`, even if the planner
   did not enumerate that dependency. A **whole-file** target is a bare path with no
   `::kind::name` segment, so it contributes no symbol of its own; since Wave 13 its
   search symbols come from the file's committed nodes instead (Wave 11's folding
   made whole-file grants the normal shape, and deriving nothing from them silently
   disabled this entire layer for them). Bounded and quality-filtered — see below.
5. **Dependency outputs** (Wave 13) — the committed source of every `target_node` of
   every task this one **directly** `depends_on`, as `read_source:<id>`. Direct
   edges only: the transitive closure grows without bound.

The agent therefore arrives with the full dependency picture — same-file context,
cross-file callers, and what its dependencies built — without the planner having to
enumerate every relationship. It still never sees the whole codebase; it sees a
semantically-bounded window.

**Why layer 5 exists.** Layers 1–4 all derive from code that *already exists*. For a
task whose targets are brand-new files — the normal shape of greenfield work — every
one of them returns nothing, and the bundle carries literally zero entries. A real
run dispatched four such tasks: one agent refused to work blind and failed loudly,
and the other three guessed. One of the guesses shipped a `pick_banner(width)` call
against a real `pick_banner(width, height)` — a `TypeError` on first use, past every
gate MAK had, reported as a completed task. `depends_on` is MAK's own assertion that
the earlier task's output matters to the later one, and by dispatch time the DAG
guarantees that output is committed and readable; nothing was reading it.

**What counts as a symbol** (layer 4, Wave 16). Only names that could be **node
ids**: functions, classes, and methods. Not module-level assignments. Wave 13's first
version took every top-level binding, which meant `__all__` — declared by most
well-formed modules — counted as a symbol, so a whole-file target on any such module
dragged in every *other* module that declared one. In the run that exposed it, one
task's bundle was 151 KB (67,847 input tokens) of which **123.7 KB matched on nothing
but `__all__`**. The rule is parity with a symbol-level target: `_file_symbols` yields
a fragmented file's literal `::method::` ids, so the AST fallback for a whole-file
node must yield the same names or the two disagree about one file depending on how it
happens to be stored.

**Layer 4's ceiling and filters** (Wave 16). The layer that spends the most had no
ceiling at all. One pass over the store now decides three things at once — the scan
uses `findall`, not `search`, because *which* symbols a node matched is what both
filters and the ranking need:

- a symbol shorter than `_MIN_SYMBOL_LEN` (4) is a word, not evidence — `run` matched
  six unrelated files in the observed run;
- a symbol matching more than `_MAX_SYMBOL_MATCHES` (8) nodes says nothing about which
  of them is related, and is discarded wholesale rather than node by node;
- survivors are ranked (most matches first, then smallest, then id) and added until
  `session.cross_file_context_bytes` (default `32000`) is spent.

Past that budget an entry is **dropped**, not digested as layer 5 does: a caller's
value *is* its call site, and a signature digest of a caller says nothing about how it
calls. The number dropped is reported as `cross_file_dropped` on the dispatch event,
so a truncated caller layer is visible rather than inferred. The two filters are
module constants and the budget is config, because the filters are claims about
*evidence* while the budget is the operator's cost dial.

**Layer 4's lookup cost** (Wave 18). The filters and the budget bound what layer 4
*sends*; nothing bounded what it *scanned*. `_scan_for_symbols` walked the entire
node store and ran a `findall` over every node's source on **every dispatch and
every retry**, so on a large repo with a wide plan it was the dominant cost of
enrichment — paid in full even when the budget then dropped almost everything it
found. It is now an inverted `symbol -> [node_id]` index built once and reused,
keyed on `NodeStore.generation` (§2) so a commit mid-wave invalidates it without
the session having to know which nodes moved.

The equivalence that makes this safe: `\bfoo\b` matches exactly where `foo` is a
maximal `\w+` run, so keying nodes by their `\w+` runs answers the same question
the regex did. Node ids yield Python identifiers, so every symbol qualifies in
practice; a symbol that is *not* a plain identifier falls back to the old scan
rather than being silently missed. **The bundle contents do not move** — same
filters, same ranking, same budget, same bytes — and the tests assert that
differentially against the implementation this replaces, because "identical, only
cheaper" is the whole claim. Measured on this repo's own 892-node store: ~8x per
dispatch, and the gap widens with store size, since the scan is O(all node bytes)
per dispatch while the lookup is O(matches).

**Budget and degradation.** Whole dependency files are the most expensive thing a
bundle can carry, so layer 5 spends a per-bundle byte budget
(`session.dependency_context_bytes`, default `24000`; `0` disables the layer, `-1`
is unbounded). Past the budget an entry **degrades to a public API digest**
(`read_api:<id>` — signatures, class members and module constants, no bodies, from
`mak/node_store/api_digest.py`) rather than being dropped: a test-writing task needs
its dependency's *contract*, not its implementation, and "informed cheaply" beats
"blind". This budget is the same one Wave 7 (planner token efficiency) exists to
control — coordinate changes to it rather than solving it twice.

**Every dispatch is on the record.** Enrichment ends by logging a `TASK_DISPATCHED`
event per attempt: task id, attempt number, targets, `depends_on`, the count of
`write_source` / `read_source` / `read_api` entries, total context bytes, and a
`starved` flag. Nothing in the kernel used to notice an empty bundle — no event, no
metric, no guard — so the only report of the defect above came from the one agent
honest enough to refuse.

Counts alone turned out to be half an answer. Each layer reports the context keys it
added, and the event carries a `layers` object — `{count, bytes, nodes}` per layer —
so **which layer put a node in the bundle is readable from the log alone** (Wave 16).
Attributing the 151 KB bundle above without it meant re-deriving the layers by hand
against `task_graph.json` and the source tree.

**The starvation guard.** A bundle that comes out of enrichment with **zero** context
entries while its task declares `depends_on` edges or `context_nodes` is a *kernel*
defect, not an agent failure. `_ConcurrentRunner.assign` never sends it: it queues a
`TaskResult(success=False, retryable=False)` naming the defect, which flows through
the normal failure reporting and fails the task immediately rather than spending
three attempts asking a model to invent an API it was never shown.

### 3.3 Collection (agent output → node store)

When an agent returns a `TaskResult`:
0. **Map the returned ids onto the grant** — `protocol.map_returned_sources`, the
   enforcement half of the node-granularity contract (see
   [7.4](#74-the-wire-protocol)). Every returned id is accounted for: granted ids
   pass through, a symbol id inside a **whole-file** grant is folded into that
   grant, and anything else is refused *and logged* (`SOURCE_DROPPED`, with the id,
   the grant, and the reason). Whatever the agent returned is recorded first as an
   `AGENT_RESULT` event. **Folded fragments are ordered, not concatenated in
   emission order (Wave 17).** Several symbols returned under one whole-file grant
   used to join in `dict` insertion order — whatever order the model happened to
   emit them — which put imports after code whenever a model wrote its functions
   first. That still `compile()`s (imports are legal anywhere), so every gate
   downstream passed it silently; only a `from __future__` import would ever have
   caught it. `map_returned_sources` now takes an optional `order_key`, and sorts
   folded fragments three ways: a `module_header` fragment always leads (a
   `from __future__` import is only legal as the first statement); otherwise the
   node store's own recorded source `order` (`NodeStore.node_order`, wired by
   `Session._stage_returned_sources`); otherwise the model's emission order as the
   fallback when the store has no opinion (e.g. the CLI bridge, which calls this
   with no `order_key` at all).
1. `compile()` each modified fragment — reject on failure. `compile()` is used (not
   `ast.parse()`) because it enforces all Python compile-time rules, including the
   requirement that `from __future__` imports appear at the very beginning of a
   module. `ast.parse()` accepts misplaced `from __future__` silently; Python's
   runtime and `compile()` do not.
2. Run the [conflict detector](#5-conflict-detector).
3. **Transactional commit** (see [Session](#10-session-lifecycle)): build the
   prospective file from committed fragments with the staged versions substituted,
   `compile()`-validate it *before* committing. Only if every affected file
   reconstructs cleanly are the fragment versions committed and the files written.
4. On success, release the task's locks and write an audit commit. On any failure,
   the whole transaction rolls back — store index, superseded fragments, metadata,
   and every output file — so the store and disk never diverge. Since Wave 19 that
   is a genuine transaction with an explicit commit point, not a best-effort
   revert; see [Session](#10-session-lifecycle).

### 3.4 Reconstruction (fragments → file) — `reconstruction.py`

`assemble_fragments(fragments)` concatenates fragments **in their stored source
order** (separated by blank lines). `reconstruct_file(...)` assembles, runs
`compile()` as a guard (enforcing all Python compile-time rules, not just
parseability), formats with `ruff format` (auto-discovering the venv's `ruff`
binary, falling back to raw source and *logging* on failure — never silently
swallowing), and writes to disk.

`ruff` is therefore a **runtime dependency**, not a dev-only tool: every file MAK
reassembles passes through `ruff format`. It was dev-only until a user installing
via `uv tool install` hit `FileNotFoundError` on every reconstruction —
`_find_ruff()` looks beside `sys.executable` then falls back to `PATH`, and an
isolated tool environment satisfies neither, so every reconstructed file was
written unformatted with only a log line to say so. The formatting fallback still
exists (a missing or broken `ruff` must never fail a run), but it is now a genuine
edge case rather than the default for an entire install method.

### 3.5 The round-trip invariant (load-bearing)

The contract that makes shared-memory editing trustworthy:

```
ingest(file) → store → reconstruct  ≡  semantically equivalent to the original,
with decorators, statement ordering, and comments intact.
```

This is verified by a property test (`tests/node_store/test_roundtrip.py`) over a
corpus that includes decorated defs, methods, module-level constants between
classes, top-level executable blocks, inline and standalone comments, and
`@overload` stubs — plus a test that MAK round-trips its *own* source. **If you
touch ingestion or reconstruction, this test is your gate.**

> **Known limitation:** a `class` shell fragment (the class line with methods
> removed) is not independently parseable, which mildly contradicts the "fragments
> parse in isolation" aspiration. This is acceptable because reconstruction
> validates the *assembled* file, not individual shells. A consequence to be aware
> of when editing methods: an agent must return method source with its original
> indentation; a dedented method would fail the assembled-file parse and be
> rejected (not corrupted) by the transactional commit gate.

## 4. Lock Manager

`mak/lock_manager/` is the concurrency arbiter.

> ### Scope: the lock table is *intra*-process, by design
>
> `LockTable` guards its state with one table-wide `threading.RLock`. That is the
> right primitive for what it actually protects — a session's own worker threads
> racing over one in-memory table — and it says **nothing** about a second process.
> Before Wave 19 that gap was load-bearing in the worst way: two `mak` runs over one
> project each built their own table over the same persistence file and both granted
> a write lock on the same node, and each startup's `clear()` dropped the other's
> leases without establishing that their owner was alive.
>
> The guarantee is **single ownership**, not a distributed lock table, and it lives
> in `project_lease.py` (§4.4). MAK does not implement distributed locking and does
> not intend to: two concurrent runs on one checkout are a mistake to report, not a
> workload to schedule.

### 4.1 Lock model

A reader-writer lock per node, with three modes:

| Mode | Concurrent holders | Use |
|---|---|---|
| `read` | unlimited | agent reads a symbol as context |
| `write` | 1 (exclusive) | agent edits a symbol |
| `intent_write` | multiple (compatible with reads, **excludes writers**) | declare a future write; deadlock-prevention signal, and (Wave 20, §4.5) the hierarchy/registrar-append mode |

The canonical conflict matrix lives in `conflicts.py` and is consumed by **both**
`RWLock.can_acquire` *and* the `DeadlockDetector`, so the two can never disagree.

### 4.2 Lock table

`LockTable` (`lock_table.py`) holds lock state in memory and persists to
`.mak/lock_table.json` after every mutation (for crash recovery). Notable methods:
`try_acquire`, `try_acquire_all` (atomic multi-lock — all-or-nothing, never partial
acquisition), `release`, `release_all`, `renew` / `renew_all` (lease heartbeat),
`expire_stale`, `clear`, and the entry accessors.

**Fresh-session hygiene:** the persisted table is for crash *recovery* within a
session. A brand-new session owns none of the leases a prior (possibly killed) run
left on disk, so `Session.initialize` calls `clear()` to drop them — otherwise they
surface later as alarming-but-spurious "lease expired: holder=… (held 1383s)"
warnings when the new run's first sweep finds them. Crash resume takes the other
path: `Session.recover` deliberately keeps the persisted table and `expire_stale`s
it.

**Concurrency model (option B):** every public mutation is guarded by one
table-wide **re-entrant lock**, so `try_acquire_all`'s check-pass and acquire-pass
cannot be interleaved by another thread. Per-node `RWLock` objects are only ever
touched while this lock is held. A concurrency stress test
(`tests/lock_manager/test_concurrency.py`) drives many threads at a shared node set
and asserts that no two conflicting holders ever coexist.

**Lease safety:** lock expiry is *observable*, not silent — an expiring lease is
logged and reported via an optional `on_expire` callback, so a scheduler can fail
and roll back the holder's task rather than have its lock vanish underneath it.
Holders keep leases alive with `renew`.

### 4.3 Deadlock detection

`DeadlockDetector` (`deadlock_detector.py`) builds a directed **wait graph** (edge
A → B means task A waits for a lock held by task B, with a conflict check),
detects cycles via an **iterative, deduplicated** DFS (`find_cycles`), and resolves
them **wound-wait** style: abort the youngest task in the cycle, release its locks,
and re-queue it.

> The lock-contention paths are now reached by the concurrent live pipeline (Wave 5),
> driven by the concurrency integration gate. The deadlock detector is wired into
> `Session.run` as a per-iteration watchdog; because the scheduler pre-allocates all
> of a task's locks atomically, a waiting task holds none, so the wait graph is
> acyclic by construction and the watchdog is defense in depth rather than a
> hot path.

### 4.4 Project lease — one owner per project (Wave 19)

`project_lease.py` provides `ProjectLease`, the *inter*-process half of the story
the lock table only covers within one process. A session takes it as the **first**
action of `initialize()` and `recover()` — before journal recovery, before
`lock_table.clear()`, before ingestion, before any state mutation — renews it on the
same heartbeat tick as the task leases, and releases it in `close()`. A second live
owner fails fast with `ProjectBusyError`, naming the holder's pid, host, session id,
and the age of its last heartbeat. Maintenance that *mutates* the store takes the
same lease: `mak gc` deletes version files and fragment directories, which
underneath a running session would remove the versions its open transaction might
need to roll back to.

**Why `flock` and not a lock file.** A lock *file* has to answer "is the owner still
alive?" from data the dead owner wrote, which is unanswerable in general: a pid can
be recycled, and a heartbeat threshold either strands live sessions or lets dead
ones block for minutes. `flock` moves that question to the kernel, which releases
the lock when the holding process dies **however** it dies, `SIGKILL` included. So
abrupt-owner recovery needs no timeout and no heuristic — the next acquire simply
succeeds. The JSON record inside the file is diagnostics (who to name in the error)
plus the staleness signal for Windows, whose `msvcrt` byte-range lock *can* outlive
its process and therefore does fall back to a `stale_after_s` threshold. That
fallback is documented as the weaker of the two paths.

Because the lease is held, `lock_table.clear()` is finally sound: holding it **is**
the proof the prior owner is dead.

### 4.5 Derived lock resources & the lock policy (Wave 20)

Every request the scheduler makes, the session re-validates at commit, and the
deadlock watchdog describes is now built by **one** function —
`mak/scheduler/lock_policy.py::lock_requests(task, policy)` — so the three can
never disagree about what a task is holding. With every `LockPolicy` flag off
it returns exactly the pre-Wave-20 set (WRITE each target, READ each
`context_node`), which is also what makes the flags a clean ablation switch for
the scaling study (Wave 21).

- **Interface/body split (`api_locks`).** `mak/lock_manager/resources.py` derives
  two resource ids per node: `<id>#api` (its interface — signature, decorators,
  return annotation, bases, fields, imports; whatever
  `api_digest.api_fingerprint` renders) and the bare `<id>` (its body). A task
  calling X takes READ on `X#api` (callees come from the pre-wave `DepGraph`,
  §8); a task that declared a body-only edit (`changes_api=False`) takes only
  `X`, so it runs beside X's callers instead of serializing with them the way a
  single node-level lock always did. An undeclared task (`changes_api=None`,
  the default) is conservative: WRITE on every target's `#api`, same as before
  this wave.
- **Intention locks (`intention_locks`).** A fragment write additionally takes
  INTENT_WRITE on its bare file id, and a method/`class_body` write on its
  `::class::` node too (`intention_parents`). A whole-file or whole-class write
  asks for WRITE at that level, which conflicts with any INTENT_WRITE below it —
  so a whole-file rewrite cannot start beside a fragment writer, while fragment
  writers of the same file still run beside each other. This is what
  `intent_write`'s multi-holder, writer-excluding semantics (§4.1) turn out to
  be for in practice.
- **Key-level registry locks (`registry_keys`).** A *registrar* function — one
  whose body is a flat list of `callee("<literal>", …)` calls, detected
  **structurally** by `mak/node_store/registrar.py`, never by name — is
  commutative when every entry is keyed. A task appending to one takes
  INTENT_WRITE on the node (co-holdable with other appenders) and WRITE on each
  declared key as `<id>#key=<literal>` (`SubTask.registry_keys`). An *unkeyed*
  list (a middleware chain, a priority list) is order-dependent and keeps the
  plain node lock — see §5.2 for what happens to the entries themselves at
  commit.
- **Recovery and the deadlock watchdog both read the same policy.** `Scheduler`
  takes an optional `lock_policy` and exposes `use_lock_policy` for
  `from_persisted` to rebuild one after reading the graph back; `Session._run_heartbeat`'s
  deadlock scan builds its wait-graph edges from `lock_requests` too, so a
  waiting task's *declared* mode (e.g. INTENT_WRITE for a registrar appender) is
  what the watchdog reasons about, not an assumed WRITE.

## 5. Conflict Detector

`mak/conflict_detector/` runs in the collection phase, between an agent's output
and its acceptance. It is **intentionally shallow** — a structural gate, not a type
checker. It gates on `compile()` success plus three checks; full correctness is the
test suite's job. (`compile()` is used rather than `ast.parse()` because it
enforces Python's compile-time rules — including `from __future__` placement — which
`ast.parse()` ignores.)

- **`signature_check.py`** — when one agent rewrites `func_b` and another's fragment
  calls `func_b`, verify the call sites are still compatible with the new signature
  (arity + keyword names). Conservative: a `*args`/`**kwargs` splat suppresses the
  checks it makes unprovable, so no false conflicts are reported. Types are never
  inspected.
- **`import_check.py`** — across concurrent `__header__` edits, flag **conflicting**
  imports (same bound name → different targets) and **duplicate** imports.
- **`name_collision_check.py`** — flag a qualified symbol (including `Class.method`)
  introduced by more than one agent in the same file/round.
- **`node_ids.py`** — the one place edit keys are parsed. Both file-local checks
  scope themselves with `file_scope_of`, and both the detector and the collision
  check attribute dedented class fragments with `class_scope_of`.
- **`detector.py`** — `ConflictDetector.detect(EditRound)` runs the parse gate then
  all three checks, returning a `ConflictReport` (`ok`, `reasons`, `by_check`).
- **`cross_module_check.py`** (Wave 13) — the one check that runs *after* a wave
  rather than inside a task. Every gate above is scoped to one agent's edit, so two
  tasks can each finish clean and still leave the codebase broken **between** them.
  `check_cross_module_api(file_sources, scope)` judges the files the wave wrote
  against the store as it now stands and reports two kinds of `CrossModuleDefect`:
  `unresolved_import` (importing a name the target module does not bind) and
  `signature_mismatch` (calling an imported function with an argument shape its
  definition rejects). It reuses `signature_check`'s extractors and
  `depgraph.resolve_module_file` for import resolution, so there is one
  implementation of each rather than two that drift, and inherits the same
  precision-over-recall contract: a call is judged only when the definition it
  reaches is provable — resolvable in-repo import, no local shadow, not a method.
  A file that does not parse yields nothing (the parse gate owns that failure).
  Import resolution runs in **strict** mode here (Wave 16): the whole dotted tail
  must match a repo path, so `pkg.mod` finds `pkg/mod.py` and `src/pkg/mod.py` but
  never `other/mod.py`. Validation's looser unique-last-segment fallback resolved
  `from PyInstaller.__main__ import run` onto a repo's own `editor/__main__.py` and
  reported correct third-party code as a defect — tolerable for a finding a human
  reviews, not for something that generates a task telling an agent to change
  working code. `Session.detect_cross_module_defects()` drives it; see §10.

### 5.1 Precision over recall (Wave 11)

A false conflict is far more expensive than a missed one: it fails the task, its
retries, and every task that depends on it — while a missed one is what the test
suite is for. A real run lost three tasks and nine dependents to a check that
rejected entirely ordinary Python, so the rules below now bound what the detector
is willing to claim.

**Decorators are read** (`Receiver`). `@staticmethod` has no implicit receiver, so
the first parameter is a *real* one and must not be stripped — stripping it made
every correct call to a static helper "pass one argument too many". `@classmethod`
does bind `cls`. A decorator that is neither recognised nor known
signature-preserving (`@lru_cache()`, `@app.route(...)`, `@x.setter`) can reshape
the callable arbitrarily, so the definition is **dropped from the table** rather
than checked against a guessed shape. Note that merely *not* stripping is not the
safe default on its own: it converts the false "too many arguments" into a false
"missing required argument 'self'".

**Attribute calls resolve by receiver, never by bare name.** `obj.get(k)` matches a
local `get` only when the receiver is `self`, `cls`, or the owning class name:

| call | resolves to | why |
| --- | --- | --- |
| `foo(...)` | module-level `foo` | methods are not keyed by bare name |
| `self.foo(...)` | `<enclosing class>.foo` | the only receiver whose type is certain |
| `cls.foo(...)` / `Owner.foo(...)` | `Owner.foo`, **if** it is a `classmethod`/`staticmethod` | an unbound `Owner.m(obj, x)` passes the receiver explicitly and is indistinguishable from a bound call |
| `self._data.get(...)`, `svc.run(...)` | nothing | the receiver's type is unknown |

The deliberate, documented cost is that a call on an untyped receiver is no longer
checked at all. The benefit is that `self._data.get(name, "")` — a *dict* `.get` —
stops resolving to the file's own `Registers.get`. Any class defining `get`, `set`,
`update`, `items`, `write`, `read`, `append`, `close`, or `pop` used to false-conflict
against ordinary stdlib calls in the same file.

**Methods are keyed `Class.method` only.** The old flat bare-name table let two
classes in one file silently shadow each other ("later definition wins").

**Fragments are re-framed before they are checked** (`detector._frame_fragment`).
The store keeps fragments dedented, so a `file.py::method::C.get` fragment reads as
a module-level `def get(self, name)` — a function with a real `self` parameter.
`method` and `class_body` fragments are therefore wrapped back in a synthetic
`class C:` (falling back to the raw source if that does not parse), which both
prevents that mis-reading and *restores* recall: `self.helper(...)` inside a method
fragment can resolve again.

**The sibling checks carry the same class of assumption**, and were audited with
these fixes:

- one edit binding a name to two targets is the conditional-import idiom
  (`try: import ujson as json` / `except ImportError: import json`), not a
  disagreement — only cross-edit disagreement is a conflict, and header edits are
  compared per file;
- symbols from a dedented `class_body`/`method` fragment are attributed to their
  owning class, so two classes in one file both defining `get` do not collide;
- a whole-file node id (`pkg/a.py`, no `::`) is its own scope, so two freshly
  created files each defining `main` are not a collision.

`tests/conflict_detector/test_false_positive_corpus.py` is the standing guard: a
corpus of correct code that must yield **zero** conflicts (including the exact
20-line source that produced the three logged false conflicts), plus a corpus of
genuine breakage that must still be reported. Add to both when you touch a check.

> The cross-agent value of these checks is now live (Wave 5): `Session._process_batch`
> validates concurrently-completing tasks together, building each task's `EditRound`
> with `definitions` spanning the whole batch (cross-agent signature authority) and
> `symbol_edits`/`header_edits` scoped to the files the task touches (name-collision
> and import checks are file-local). A task that collides with a batch peer already
> committed ahead of it is rejected and retried.

### 5.2 Semantic conflicts (Wave 20)

Node-level write locks guarantee that **no two agents write the same AST node at
the same time** — a *textual* guarantee, enforced by construction. They say
nothing about two edits on *disjoint* nodes that are each correct alone and
wrong together: a task builds on a sibling's return value while that sibling
is rewritten underneath it, a signature changes under a call the plan never
saw, two tasks each register the same key in a shared table. PLANS §5.1 had
listed a cycle-free-dependency-graph check that nothing implemented, and three
more shapes had no check at all. Wave 20 closes the gap across three layers —
prevent at scheduling time, detect at commit and at wave end, resolve by
re-dispatch or fix-up — all living in the new `mak/semantic/` package plus six
new modules under `mak/conflict_detector/`.

#### Prevention

- **Read-set versioning (`mak/semantic/read_set.py`).** Before this wave, only
  the planner's `context_nodes` were read-locked or tracked at all — everything
  `_enrich_bundle` added on its own (same-file siblings, cross-file callers,
  dependency outputs, §3.2) was invisible to the kernel, and a task could be
  rewritten underneath a sibling with no way for the commit to know. A `ReadMark`
  now records, for **every** context key a bundle carries, the node's committed
  version and a **content digest** — the digest, not the version, is the
  identity that matters, because a node that is uncommitted or retired-and-
  recreated restarts at version 1 with different content (the classic ABA
  problem). `build_read_set` derives the whole set structurally from the
  enriched context and the per-layer attribution `TASK_DISPATCHED` already logs
  (§3.2), so a layer added later is covered without anyone remembering to wire
  it in. The set is captured on the dispatching thread immediately after
  enrichment (`Session._record_read_set`) — commits happen on that same thread,
  so nothing can advance the store between reading sources into the bundle and
  stamping them — and persisted alongside the task graph (`Scheduler.annotations`)
  so `--recover` does not lose it.
- **Interface/body lock split, intention locks, key-level registry locks** —
  see §4.5. Callers read-lock a callee's `#api`; a declared body-only writer
  takes only the body and runs beside its callers; a whole-file/whole-class
  write cannot start beside a fragment writer below it; a keyed registrar's
  appenders co-hold it and each declared key locks separately.
- **Declared contracts (`mak/planner/contracts.py`, `mak/semantic/contracts.py`).**
  A task may declare, per target, the signature it will give it —
  `contract: {node_id: "def f(a: int) -> R"}` — plus `changes_api` and
  `api_targets` (§8 has the planner-schema side) and `registry_keys` (the keys
  it will append, for the lock above). Every declaration is a promise the
  kernel **enforces at commit, never trusts**: `Session._contracts_hold`
  compares the committed source against `implementation_mismatch(contract,
  source)`, which parses both to a canonical signature (name, parameters with
  annotations and defaults, return annotation, async-ness, or a class's bases)
  and refuses a drift with a note naming exactly what changed. A dependent is
  shown its providers' (and its own) contracts as **layer 0** of its bundle
  (`contract:<id>` context keys, rendered by `render_contract` with a role —
  "you must implement this" vs. "build against this fixed signature") — before
  the provider's code even exists.
  With `semantic.contract_dispatch` on (opt-in, off by default), a dependency
  edge whose provider fully declares a contract for every node it writes, and
  whose lock set does not conflict with the dependent's, becomes **soft**
  (`mak/semantic/contracts.py::soft_edges`, `DAG.soft_dependencies` — the DAG's
  `newly_unblocked` treats a soft dependency as satisfied for dispatch, but
  batch/topological ordering still respects it): the dependent is dispatched
  against the contract while the provider is still being implemented, and its
  commit is *parked* (below) until the provider's commits. If the provider
  fails, the dependent fails with it rather than committing against an
  interface that was never built.
- **Plan validation reads the declarations too (`mak/planner/validation.py`,
  P4).** With `PlanSemantics` supplied, `validate_plan` **relaxes** the edge
  from a writer that declared a node body-only or narrowed its API change
  elsewhere (`relaxed_dep` — the `#api` lock and the commit-time check now
  carry that guarantee, so the edge only cost parallelism); **adds** an edge
  from a task that declares an API change to every task whose description,
  context or contract names that node (`declared_api_dep`) — the only way to
  see a caller the reference graph cannot, because the call does not exist
  yet; orders a class's structure writer (its `::class::` node or `__init__`)
  before tasks writing its other members (`shared_structure`); and flags (never
  silently merges) two tasks declaring the **same** registry key on one table
  (`registry_key_collision`, ordered so the second would visibly overwrite the
  first) or appending to an **ordered** (unkeyed) registrar at all
  (`ordered_table` — order is meaning there, and no lock can choose it, so the
  finding asks the plan to say where each entry belongs).

#### Detection — at commit

`Session._validate_and_commit` runs, in order: **(1)** the registrar merge
(below), **(2)** read-set validation, **(3)** the structural checks (syntax,
signatures, imports, name collisions, and now a **duplicate registry key**
check), **(4)** the contract check, **(5)** interface enforcement. Any step
can reject or defer the commit; nothing after it runs.

- **Keyed-registrar reconciliation (`mak/node_store/registrar.py`,
  `mak/semantic/registry_merge.py`).** A registrar is detected by *structure*
  — an optional docstring, a prelude of simple assignments, a flat run of
  `callee("<literal>", …)` statements, an optional `return`; placeholder
  bodies (`pass`, `...`, `raise NotImplementedError`, or an empty local table
  built and returned in the prelude) count as an empty table — never by the
  name `_register_all`, so it generalizes to any project's wiring function.
  Two appenders holding a table's INTENT_WRITE each return the table *as they
  read it*, plus their own lines; committing either as-is would silently drop
  the other's. `plan_merge` instead extracts what an agent **appended** to the
  version it read (`appended_entries`) and replays exactly those entries onto
  whatever the table holds **now** (`merge_append`) — a pure textual splice
  after the last existing entry (or in place of the stub), so formatting and
  comments survive. Anything that is not a pure keyed append — an edited or
  removed entry, an unkeyed entry, a table that stopped being a registrar, or
  a table that moved since the agent read it in a way an overwrite would lose
  — cannot merge: the commit takes the node's plain WRITE lock instead
  (waiting if another appender holds it) or is sent back with a fresh read if
  even that would lose entries. `check_registry_keys`
  (`mak/conflict_detector/registry_key_check.py`) then reports a key
  registered twice **by this edit** (comparing against the table's *previous*
  committed source, so pre-existing debt is not blamed on the task that
  happened to touch the file) as a `registry_key` conflict — shape 6, and the
  one place a kernel can do strictly better than a textual merge, which
  applies both lines and lets the second silently win at runtime.
- **Stale-read validation (`mak/semantic/stale.py`, D1/R1).** Every node in the
  task's read set is compared, by digest, against what is committed now.
  Nothing stale → proceed. Something stale → `classify` reads it as
  `body_only` (the interface fingerprint is unchanged — and "interface"
  here means *binding-level*: a node that only **gained** a name, a new
  import, a new sibling helper, broke nobody, because nothing could have
  depended on a name that did not exist; `mak/semantic/interface.py`'s
  `changed_bindings` is what makes that distinction, replacing the coarser
  "any fingerprint diff" rule), `api_change`, `deleted`, or `created` — and
  whether the task's own staged code actually **references** anything that
  changed (`_referenced`; an unreferenced change is free to accept, however it
  changed). `semantic.stale_read` then decides:

  | Policy | body-only | referenced API change |
  |---|---|---|
  | `accept_if_api_stable` | accept | re-dispatch |
  | `revalidate` (default) | accept | re-verify the static checks against the *new* code; a parameter-shape-only change to a module-level function/constructor is accepted if they pass; ask the adjudicator if configured; otherwise re-dispatch |
  | `redispatch` | re-dispatch (strict snapshot isolation) | re-dispatch |
  | `reject` | accept | reject, like any other conflict |

  A node the task only saw as an API digest (`read_api:`, past the dependency
  context budget, §3.2) never re-dispatches on a body change — it was never
  shown the body to begin with. **Every stale read is logged** (`STALE_READ`)
  with the node, both versions, the change kind, whether it was referenced,
  and the verdict — this is an acceptance criterion, not a debugging aid. A
  node covered by a declared contract the task was built against is accepted
  outright, whatever else changed about it: the contract, not the
  implementation, is the authority the task answers to. A re-dispatch carries
  a **bounded unified diff** of every blocking node (`retry_note`, R1) and
  counts against the ordinary `max_attempts` budget with
  `error_kind="stale_read"` — this is a semantic rebase done by the agent, at
  the cost of one call, not a full attempt burned re-asking an identical
  question.
- **Interface enforcement (`mak/lock_manager/resources.py`, §4.5).** A task
  that declared `changes_api=False` and then changed an *existing* binding
  anyway is refused outright (its callers ran beside it on that promise). Any
  other undeclared interface change needs `#api` WRITE: taken on the spot if
  free (logged `API_ESCALATED(outcome=acquired)`); if a concurrent task holds
  it as a reader, the **commit is parked**, not the agent re-run — the readers
  will validate their own commits against whatever this one leaves behind, so
  the writer only has to wait for none of them to still be mid-build.
- **Parked commits.** A finished result that cannot commit *yet* — waiting on
  a registrar's exclusive lock, on an `#api` reader, or on a contract-dispatch
  provider — is parked (`Session._park`/`_resume_parked`, `COMMIT_DEFERRED`),
  not re-dispatched: the agent's work was fine, only the timing was wrong, and
  re-running it would spend a whole call and an attempt of the retry budget
  to arrive at the same answer. Every batch completion retries every parked
  result (`_resume_parked`); if every task still in flight is parked (nothing
  can make progress), the run loop breaks the tie by releasing the
  highest-id victim — re-gated on its dependencies if it was waiting on a
  contract provider, re-dispatched with a note if it was waiting on a lock —
  which is the one place a wait in this design can become a cycle at all
  (parking is the only state where a task waits *while holding locks*).

#### Detection — at wave end and optional gates

`detect_cross_module_defects()` now runs four more whole-repository checks
alongside the Wave 13 unresolved-import/arity one, all reading the touched
files through `mak/semantic/module_index.py::ModuleIndex` (one shared
import-resolution and class-lookup layer, built on the same *strict*
`resolve_module_file` §8 uses, so the checks cannot disagree with each other
about what a module binds):

- **`attribute_check.py`** — `mod.name` where `mod` resolves to an in-repo
  module that no longer binds `name` (shape 4: a rename or deletion caught
  through a module alias, which the from-import check never reads). Skips a
  module that binds names dynamically (`__getattr__`, a star import,
  `globals()`/`exec`), a rebound local, and anything not in `Load` context.
- **`override_check.py`** — an override that cannot accept what its base
  method accepts (shape 5), checked positionally and by keyword against the
  base's `Signature` (shared with `signature_check.py`'s `signature_for`).
  Skips constructors and other non-Liskov dunders, a static/class/instance
  receiver mismatch, a renamed positional-or-keyword parameter, and any base
  that does not resolve in-repo.
- **`constructor_check.py`** — a call to an in-repo class whose constructor
  rejects it (shape 8): an explicit `__init__` (own or inherited), or a
  `@dataclass`'s fields (bases first, `ClassVar`/`field(init=False)` excluded,
  defaults and `kw_only`/`KW_ONLY` read). Skips any class with a metaclass, a
  `__new__`, multiple resolved bases, or an unrecognised decorator.
- **`cycle_check.py`** — the PLANS §5.1 check that never existed: a **new**
  module-level import cycle among files the wave touched, found via an
  iterative Tarjan SCC over the module-level import graph and reported only
  when it contains a `from`-import edge (a plain `import pkg.mod` cycle
  usually works at runtime and is left alone). Function-local and
  `TYPE_CHECKING`-guarded imports are excluded — they cannot deadlock module
  initialisation.
- **`duplicate_check.py`** — the same top-level function, created by
  **different tasks** in different files this wave, with an equivalent body
  once docstrings are dropped (shape 9 — two agents each writing a private
  `_normalize_email`). Conventional names (`main`, `run`, `test*`, dunders)
  are never reported.

Every check above (and the Wave 13 one) also runs against the **pre-wave**
state, and only a defect *absent* there is reported — pre-existing debt is
never blamed on the wave that merely touched the file. Findings are cached per
store `generation`, since the cascade loop asks twice for the same state.

`detect_cascade_tasks()` — the same entry point as before, now assembling from
three sources instead of two, folded into one task per node
(`_merge_fixups`):

- **cascade, rewritten onto the real graph (`mak/semantic/cascade_graph.py`,
  R3).** The old check compared each committed node's *first function
  signature* before/after and then regex-matched `symbol` across other
  files — blind to same-file callers, to a deleted symbol (there was no "new"
  signature to compare), and to anything a node id didn't map onto
  one-to-one. This wave diffs **symbols**, not node ids
  (`mak/semantic/symbols.py::diff_symbols` — a per-file before/after table that
  sees a signature change, a deletion, or a body change the same way whether
  the file is stored as fragments or one whole-file node), and walks the
  **reference graph** for callers: the pre-wave graph (so a deleted symbol's
  existing callers are still found) and the graph rebuilt after the wave (so a
  caller the wave itself added is found too), same-file callers included. A
  caller whose calls are *provably* compatible with the new signature (the
  shared `check_call`) is left alone rather than given needless fix-up work. A
  deleted symbol's fix-up description names a same-bodied symbol the wave
  added instead, when one exists, as a rename hint.
- **cross-module defects**, as before plus the four new checks above.
- **optional gates (`mak/semantic/gates.py`, D3/D4/D6), all off by default and
  none able to fail a wave** — a finding becomes a fix-up task exactly like
  the others, and a gate whose tool is missing or times out is logged
  (`GATE_FINDING`) and skipped, because its infrastructure failing says
  nothing about the wave's code:
  - **`type_gate.py`** diffs pyright/mypy diagnostics (`semantic.type_check`)
    over the touched files plus their static importers against a
    **baseline** taken once at `initialize()`, so a codebase that was never
    type-clean is judged only on what the wave *introduced*. Discovery
    mirrors the venv-`ruff` rule (§3.4): the binary beside the running
    interpreter first, then `PATH`.
  - **`impact_tests.py`** (`semantic.impact_tests`) selects the tests whose
    static import closure reaches a touched module, runs them on the wave's
    end state and on the pre-wave state (both materialized without git via
    `mak/semantic/overlay.py`, which copies the work dir with chosen files
    substituted or removed), and for each **new** failure attributes it to
    the smallest task or task **pair** that reproduces it — first a single
    task's commits alone, then every pair, up to
    `semantic.impact_max_overlays` overlays; whatever the budget cannot
    narrow is blamed on every task that touched an importer. A test passing
    with task A alone and task B alone and failing with both is the standard
    research definition of a semantic merge conflict, and this is the one
    detector that can actually evaluate it, because any subset of a wave's
    commits is assemblable from the store without git. `WaveView.subset`
    rebuilds "pre-wave plus only these tasks' commits" from the per-commit
    fragment log kept during the run (`_wave_fragments_before`,
    `_wave_commit_log`), not from files — a whole-file commit supersedes the
    fragments before it in the rebuild exactly as `commit_node` does live.
  - **`import_smoke.py`** (`semantic.import_smoke`) imports every touched
    module in a fresh subprocess, before and after the wave, and reports one
    that stopped importing cleanly — the parse gate proves a module is valid
    Python, not that it can be *imported* (a name that fails at import time,
    a module-level call into code another task changed, a cycle that only
    bites on first import).
  - **`adjudicator.py`** (`semantic.adjudicator: "<backend>:<model>"`, D7) asks
    a cheap model one question — "does B's use of X still hold under A's
    change?" — for a stale read the static checks in `revalidate` could not
    settle, budgeted (`adjudicator_max_calls`) and logged (`ADJUDICATION`)
    per call. It can only ever turn an uncertain re-dispatch into an accept;
    a "no", an unparsed answer, an exhausted budget, or a call failure all
    leave the re-dispatch standing, and it is never consulted once a static
    check has already found a defect.

#### Resolution

- **R1** is the stale-read re-dispatch above.
- **R2** — every fix-up task, cross-module, cascade, or gate-sourced, now
  names the **task(s)** whose work met in the defect and (for the
  cross-module case, `_pair_context`) carries a **bounded diff of both
  sides** of the wave, not just the defining module's current source. Before
  this the fix-up only got the two files as they stand; now it is told what
  each side actually changed and by whom.
- **R3** is cascade on the real graph, above.
- **R4** (revert as an alternative to a fix-up) was scoped in the wave's
  design notes but not built — the store already keeps ≥2 versions per node
  (§2), so `revert_node` is available to a front end that wants to offer it;
  nothing in the kernel calls it automatically today.

#### The corpus (`benchmark/semantic/`, 20.1)

One scenario per shape 1–9 (shape 10 — config, SQL, docs — is not
representable: those files are not nodes, so nothing here covers them, and
the table says so explicitly). Each scenario is a tiny project, two scripted
edits A and B (`ScriptedAgent`, with a timing hook that holds B's first call
until A's commit is on record — but only if the kernel actually dispatched
them concurrently; if it serialized them there is nothing to wait for), and
an oracle whose contract is checked by `evaluate.validate`: it must pass on
the base project, on A alone, and on B alone, and **fail** on the naive
combination (a three-way `git merge-file --union`, B's side first).
`evaluate.run_mak` drives a real `Session`; `evaluate.run_worktrees` merges
the two single-edit states the way a worktree agent would leave them (text
spliced by symbol span, not a full-file rewrite, so the merge sees only what
each edit actually changed). Measured by `benchmark/semantic/run_semantic.py`:

| shape | scenario | MAK | worktrees | false positives | extra calls |
|---|---|---|---|---|---|
| 1 | stale read (write skew) | detected (commit) | missed | 0 | 1 |
| 2 | signature change vs new call | detected (wave end) | missed | 0 | 0 |
| 3 | behaviour change, same signature | missed / with `impact_tests`: detected (wave end) | detected (CI tests) | 0 | 0 |
| 4 | deletion / rename | detected (wave end) | missed | 0 | 0 |
| 5 | override against a changed base | detected (wave end) | missed | 0 | 0 |
| 6 | duplicate registration key | detected (commit) | detected (textual conflict) | 0 | 1 |
| 7 | order-dependent table | **prevented** | detected (textual conflict) | 0 | 0 |
| 8 | new required field vs new construction | detected (wave end) | missed | 0 | 0 |
| 9 | duplicate implementation | detected (wave end) | missed | 0 | 0 |
| 10 | out-of-store artifacts | not representable | not representable | — | — |

Every shape is prevented or detected under MAK (shape 3 needs the
`impact_tests` gate — a behaviour change behind an unchanged signature is
invisible to every static check by construction); worktrees miss six of nine
because a textual merge has no way to see a semantic one. Zero false
positives across the corpus's single-edit runs (a rejection, re-dispatch, or
fix-up on a *lone correct edit* would mean a check was firing on nothing),
and at most one extra agent call per detected shape — the cost of the one
re-dispatch stale-read detection needs (shape 1); everything caught at wave
end costs a fix-up wave instead, which is reviewed like any other cascade,
not silently spent. `tests/test_semantic_corpus.py` is the standing gate on
both the table and the corpus's own validity contract.

Separately, `tests/test_wave20_acceptance.py` runs the four benchmark
templates (§Benchmark) through a real (mocked) MAK session and asserts **zero**
rejections, stale-read re-dispatches, or fix-up tasks on a clean run — the
false-positive guard on real-shaped work, not just the seeded corpus — and
that Template 3/4 wall-clock with the interface/body split on is not worse
than with every Wave 20 lock flag off.

## 6. Scheduler

`mak/scheduler/` turns the planner's plan into running work.

- **`dag.py`** — `DAG` builds the directed graph from `SubTask.depends_on` and
  **validates at construction**: unique ids, every dependency references a known
  task, no self-edges, acyclic (Kahn's algorithm) → `SchedulingError` otherwise.
  Exposes a deterministic `topological_order()`, `mark_complete()`, and
  `newly_unblocked()` (hands out each task exactly once; the first call yields the
  initial ready set).
- **`scheduler.py`** — `Scheduler.tick()` drains the ready queue under **atomic lock
  pre-allocation**: before dispatching a task it acquires *all* of the task's write
  locks in one `try_acquire_all`. If any lock is unavailable the task stays ready
  and is retried next tick — partial acquisition (the classic deadlock setup) never
  happens. `on_task_complete` releases locks, marks the DAG edge, and extends the
  ready queue; `on_task_failed` optionally re-queues. Execution state persists to
  `.mak/task_graph.json` after every transition; `from_persisted(...)` rebuilds the
  scheduler for crash recovery (re-queuing in-flight tasks). Collaborators (lock
  manager, registry, agent runner) are injected behind `Protocol`s for mock-based
  testing.

## 7. Agent Runner & Adapters

`mak/agent_runner/` is the boundary to the actual agents. The key principle: **the
kernel never calls a model API directly.** It speaks to an `AgentAdapter`, which
translates between MAK's protocol and a specific backend.

### 7.1 The adapter interface

- `AgentAdapter` (ABC, `adapters/base_adapter.py`) — transport-agnostic. Methods:
  `format_task(bundle) -> str`, `parse_result(raw) -> TaskResult`,
  `health_check() -> bool`. This is all an API adapter needs.
- `SubprocessAgentAdapter` (ABC) — adds `spawn(working_dir) -> Popen` for CLI
  adapters, so API adapters aren't forced to implement a meaningless subprocess
  method.

### 7.2 API adapters are primary

> **Design decision:** the primary adapters are **direct API integrations**, not CLI
> subprocess wrappers. CLI stdout scraping is brittle against upstream format
> changes; direct API calls return structured JSON natively. Equally important,
> MAK's agent contract is a *pure fragment transform* (one node in → one strict
> `TaskResult` out) — the kernel owns planning, locking, conflict detection,
> reconstruction, and git. Autonomous file-editing agents (which roam the repo, edit
> disk, run tests, commit) conflict with the node-store-as-source-of-truth model. A
> single structured API call is the right shape; an autonomous agent loop is not.

The three built-in API adapters all **force structured output** so the model cannot
reply with prose:

| Adapter (`agent_type`) | Backend | How structured output is forced |
|---|---|---|
| `anthropic_api` (primary) | Anthropic Messages API | `tool_choice` pinned to a `submit_task_result` tool whose schema is the `TaskResult` shape |
| `openai_api` | OpenAI Chat Completions | JSON mode (`response_format={"type": "json_object"}`) |
| `gemini_api` | Google GenAI `generate_content` | function-calling config in `ANY` mode restricted to `submit_task_result` |

The SDKs (`anthropic`, `openai`, `google-genai`) are declared dependencies, but each
adapter imports its SDK **lazily** and accepts an **injectable client** — so import
time stays fast and the tests never make a real call (they inject fakes).

The whole new source of every changed node travels back inside that one structured
reply, so a node's full file can be large — especially a **whole-file node** (§2)
returning an entire new module.

### 7.2.1 Output budget and truncation safety (Wave 12)

**The defect.** A truncated structured reply and a deliberate "nothing to change"
are byte-identical by the time the session sees them: both are a `success=True`
result with no fragments. Before Wave 12, `AnthropicApiAdapter` requested a
hardcoded `max_tokens=8192` — about 6% of `claude-sonnet-5`'s documented output
limit, and just under the size of the whole-file rewrites this project actually
asks agents to produce (a 26 KB module costs roughly 7,900 output tokens once
JSON-escaped). A reply cut at that cap arrives as a `tool_use` block holding only
the scalar fields that finished (`{task_id, success}`), which decoded into a
perfectly valid *successful* empty result — reported as a completed task for
work that was never done. A real run against a Vim-clone codebase completed 4
tasks and failed 2, but 2 of those 4 "completions" had received no work at all;
real progress was 2 tasks out of 20.

**The fix, adapter by adapter.** All three API adapters now share a uniform
contract via two new modules:

- `mak/core/budget.py` — `resolve_output_budget(model, *, fallback, minimum,
  maximum)`, the catalog lookup lifted out of the planner's
  `resolve_max_tokens` (§8) so an adapter is not reaching into `mak.planner.*`
  (the wrong dependency direction). `mak/planner/llm.py::resolve_max_tokens` is
  now a thin delegate over this with the planner's own clamp — its behavior and
  public name are unchanged.
- `mak/agent_runner/adapters/budget.py` — `resolve_agent_max_tokens(model)`,
  the same resolver with an **agent-shaped clamp**: floor 8192 (above the
  largest single-file rewrite this project has needed), ceiling 32000 (matches
  the planner's, and keeps the request inside the SDKs' non-streaming window),
  fallback 16384 for a model the catalog doesn't know. It also declares the
  provider stop-signal vocabulary: `TRUNCATION_STOP_REASONS` (`max_tokens`,
  `length`, `MAX_TOKENS`) and `REFUSAL_STOP_REASONS` (`refusal`,
  `content_filter`, `SAFETY`, `RECITATION`, `PROHIBITED_CONTENT`, `BLOCKLIST`).
- `mak/agent_runner/stop_signals.py` — `check_stop_reason(stop_reason, ...)`
  raises `AgentTruncatedError`/`AgentRefusedError` *before* any caller can read
  a partial payload as a result; `extract_usage(...)` normalizes each
  provider's differently-named token counts into `{input_tokens,
  output_tokens, total_tokens}`; `with_response_metadata(...)` merges the stop
  reason and usage into the JSON payload the protocol decoder sees, so a
  *good* attempt carries them too.

| Adapter | Budget | Streaming | Stop-signal check |
|---|---|---|---|
| `anthropic_api` | `resolve_agent_max_tokens(model)` — **32000** for `claude-sonnet-5`/`claude-opus-5`, not 8192 | `messages.stream(...)` + `get_final_message()` — the same trap the planner hotfix already hit: past a real budget the SDK refuses a non-streaming call ("Streaming is required for operations that may take longer than 10 minutes") | `stop_reason` checked **before** `_extract_tool_payload` reads `block.input`, so a cut-mid-array payload is never read as a result |
| `openai_api` | none sent unless `max_tokens` is configured — inherits the model's own maximum, which Wave 12 judged the better default | unchanged (`chat.completions.create`) | `choices[0].finish_reason == "length"` → truncated; `"content_filter"` → refusal. A length-truncated JSON-mode reply usually fails as invalid JSON already, but "usually" was not a contract — a cut landing on a closing brace would otherwise decode clean |
| `gemini_api` | none sent unless `max_tokens` is configured (as `max_output_tokens`) | unchanged (`generate_content`) | `candidate.finish_reason` containing `MAX_TOKENS` (string-compared, since the SDK's enum `str()` is dotted) → truncated; `SAFETY`/`RECITATION`/`PROHIBITED_CONTENT` → refusal |

Each adapter's forced-output schema (and CLI-bridge prompt, §7.2/§7.4) also
gained a `no_changes_required` boolean the model must set to assert a no-op —
see §10 for why an *absence* of fragments is no longer enough — and every
prompt now honours an optional `retry_note` on the bundle (§7.4, §10).

`AgentConfig.max_tokens: int | None = None` (§11) lets an operator override the
resolved budget per agent — `None` keeps the catalog/no-cap default described
above.

CLI subprocess adapters (`claude_code`, `codex`, `copilot`) are a **secondary
fallback**, implemented over a shared `CliSubprocessAdapter` base (`cli_adapter.py`).
Real CLIs (`claude`, `codex`, `gh copilot`) don't speak MAK's newline-JSON protocol,
so each adapter launches a **bridge wrapper** — `python -m
mak.agent_runner.wrappers.<name>` — instead of the raw binary. The wrapper
(`mak/agent_runner/wrappers/`) reads a `TaskBundle` line, turns it into a prompt that
asks the CLI for the rewritten source of each target node as a strict JSON object,
invokes the CLI non-interactively, and writes back a `TaskResult` line. The `cmd`
override selects which underlying binary the wrapper drives (passed through as
`--cli <binary>`), and the invocation is further overridable per wrapper via
`MAK_<AGENT>_CMD` (e.g. `MAK_CLAUDE_CODE_CMD="claude -p --model …"`). These run over a
pooled subprocess and can be Docker-sandboxed (§7.6). The API adapters remain primary;
see §14 for how to actually use a CLI agent.

**Health check at dispatch.** Every adapter implements `health_check()`, and the
composition root now *calls* it: `bootstrap.healthy_agent_types(registry, types)`
health-checks each configured agent once at startup (`build_session`). For a CLI
adapter this runs the wrapper's `--health-check` mode (which verifies the binary is on
PATH); for an API adapter it confirms the SDK client constructs. Unhealthy agents are
dropped from the run (with a warning) and the run aborts if the *default* agent is
unusable — so a missing CLI or absent key fails fast instead of hanging until the
task timeout.

### 7.3 Registry and composition root

- `AdapterRegistry` (`registry.py`) — an **instance**, never module-global mutable
  state. `register(agent_type, cls)` registers a zero-arg class; `register_factory(
  agent_type, factory)` registers a callable that builds a *configured* adapter
  (this is how a configured `model` + API key reach an adapter, which a bare class
  can't carry). `get(agent_type)` resolves and instantiates, raising
  `UnknownAgentTypeError` for unknown types.
- `bootstrap.py` (the **composition root**) — `build_registry(config)` registers a
  config-bound factory per agent type (binding each agent's configured `model` and
  the API key resolved from its `api_key_env` at build time; SDK clients are still
  built lazily, so this performs **no network call**). CLI types get a factory bound
  to their `cmd` and an optional sandbox; an unknown type resolves to a clear error.
  `default_agent_type(config)` returns the routing default (the first configured
  agent), and `validate_config(config)` rejects unknown agent types at startup.
  `agents_from_specs(specs)` builds a roster of `AgentConfig`s from CLI
  `provider[:model]` strings (the `--models` flag, §12.1), mapping friendly provider
  names to adapter types + key env vars via `_PROVIDER_TO_API`; `SUPPORTED_PROVIDERS`
  and `DEFAULT_KEY_ENV` are the public knobs the CLI reuses. `mak/__main__.py` is the
  thin CLI shell over these functions.

**Keyed by agent id, not adapter type (Wave 22).** `_claim` used to key the
registry by `agent_type` — plain dict assignment, so a second entry of the same
type silently replaced the first and one of the two agents never ran, with
nothing said about it. `register`/`register_factory` now key by `AgentConfig.id`
(defaulted from `type` for a legacy config, so nothing written before Wave 22
changes behavior) and `_claim` raises a `ConfigError` on a duplicate id instead of
overwriting. This is what makes two endpoints on the same transport — two
OpenAI-compatible services, or two models on one of them — coexist in one
registry; see "Universal OpenAI-compatible endpoints" in the history section
below for the full design. `replace_factory` is the one deliberate exception,
for tests that need to swap a registered double; `list_ids()` returns every
registered id and `list_types()` is kept as a deprecated alias.

### 7.4 The wire protocol

`protocol.py` defines the single canonical wire schema — exactly the `TaskBundle` /
`TaskResult` dataclasses, serialized as newline-delimited JSON with
`protocol_version` `"1.0"`. `decode_task_bundle` rebuilds nested `LockEntry` /
`ResourceRef` objects rather than leaving raw dicts.

**The agent's rewritten source travels on the result.** `TaskResult.new_sources` maps
each changed node id to its full new source. `decode_task_result` accepts three
shapes and normalizes them into that field: `modified_nodes` (ids only — source
staged out of band, e.g. by a local test runner), a `modified_fragments` array of
`{node_id, new_source}` (what the API adapters elicit from the model), or an explicit
`new_sources` map. The session stages each returned source via `put_node` before the
commit phase (§10), so a real agent's edit reaches the store through the normal
transactional path.

**Decode hardening (Wave 12).** A live run hit
`TypeError: string indices must be integers, not 'str'` when a model returned
`modified_fragments` as a **single object** rather than an array — iterating a
dict yields its keys, so `fragment["node_id"]` indexed a string. The runner's
blanket `except Exception` then reported this as `"api call failed: …"`, blaming
the transport for what was actually a decode of a well-formed HTTP response, and
it cost that task a whole attempt. `decode_task_result` now:

- coerces a lone `modified_fragments` object into a one-element list (an
  obviously-intended shape) and rejects anything else that isn't an array, naming
  the field and the type received;
- coerces `modified_fragments` **JSON-encoded into a string** by parsing it once
  and validating the result normally. This is the second occurrence of the same
  class: a later run lost a task and stranded twelve dependents behind it on
  three identical `got string` rejections, ~18k output tokens spent re-earning
  the same answer, while the array sat inside the string. A string that is *not*
  JSON stays a rejection — reading it as "the source of my one target" would mean
  inventing the node id it belongs to, and the decoder does not guess; the retry
  note states the required shape instead (§10);
- includes a bounded, whitespace-collapsed **excerpt of the rejected value** in
  the message, so the log says what arrived and not only its type. Reporting
  `got string` alone made that run's failure undiagnosable after the fact: the
  artifacts could not distinguish a JSON-encoded array (recoverable) from raw
  source text (not);
- validates every fragment is an object with a non-empty string `node_id` and
  (if present) a string `new_source`, and that `new_sources` is a mapping of
  string values — each violation raises `AgentProtocolError` naming exactly
  what was expected and what arrived;
- replaces the bare `data["task_id"]` / `data["success"]` `KeyError`s (exactly
  what a truncated tool_use produces — the fields that finished, nothing else)
  with the same descriptive failure;
- decodes the new `no_changes_required` / `stop_reason` / `usage` / `retryable`
  fields (§1) into the `TaskResult`.

No malformed shape can escape `decode_task_result` as a raw `TypeError` or
`KeyError` any more — every one is a named `AgentProtocolError`, which
`AgentRunner` (§7.5) reports as a decode failure rather than a transport one. The
raised error also **keeps the provider's `stop_reason` and `usage`**: the adapter
merges them into the payload before decoding, so they are in hand even when the
body is not, and a rejected attempt still accounts for the tokens it burned
instead of logging `usage={}`.

**The node-granularity contract.** MAK grants write locks per node id, so an id it
did not grant is an id it cannot safely apply. Both halves of that rule live in
`protocol.py`:

- `NODE_ID_CONTRACT` is the sentence every adapter's system prompt (and the CLI
  bridge's prompt, and the `node_id` field description in the Anthropic/Gemini tool
  schemas) states to the model: copy ids verbatim from `target_nodes`, never invent
  narrower or broader ones, and return a bare-path target as one complete file.
- `map_returned_sources(grant, new_sources)` enforces it, and is shared by the
  session and the CLI bridge so one rule governs every path that receives agent
  output. A returned id either **is** granted, or names a symbol inside a file
  granted as a whole-file node — in which case it is **folded into that grant**
  (several fragments concatenate in the order returned; an explicit whole-file
  source wins) — or it is refused, with the reason handed back to the caller to
  log. The reverse mismatch is *not* forgiven: a whole-file rewrite returned under a
  fragment grant would touch nodes belonging to other tasks.

Folding exists because dropping was worse. A greenfield task granted
`editor/motions.py` whose agent answered with `editor/motions.py::function::move_word`
had every fragment discarded silently, retried three times, and failed — taking its
dependents with it.

**Two more contracts every adapter's prompt states (Wave 12), alongside
`NODE_ID_CONTRACT`:**

- `NO_CHANGE_CONTRACT` — a no-op is accepted only when the agent *sets
  `no_changes_required`*, never merely by omission (§10 explains why an absence
  of fragments stopped being sufficient evidence).
- `RETRY_NOTE_CONTRACT` — if the bundle carries a `retry_note` (populated by the
  session on a re-dispatch, §10), follow its instruction instead of repeating
  the attempt that produced nothing.

### 7.5 The runner

`AgentRunner.assign(adapter, task)` (`runner.py`) is the single entry point and
routes by adapter type:
- **API adapters** (primary): `format_task → send → parse_result`.
- **Subprocess adapters** (the CLI path): driven over an idle-process pool
  per agent type — write the task as a JSON line, read the result back under a
  timeout (the reader tolerates noisy preamble and multiline pretty-printed JSON),
  SIGTERM on timeout, discard a process on failure rather than returning it to the
  pool. The runner's read timeout comes from the largest configured agent `timeout`,
  and each agent type's `max_instances` caps its retained idle-process pool
  (`AgentRunner(timeout_s=…, pool_caps=…)`, wired in `build_session`).

Every path returns a `TaskResult`: backend failures become `success=False` (so the
scheduler can re-queue); a genuinely misconfigured adapter raises `AgentError`.
`shutdown()` drains the pool.

**The runner owns the work dir, and every provider client is bounded (Wave
17).** `assign(adapter, task, working_dir=None)` used to default `working_dir` to
`"."`, and the *caller* inside `Session` (`_ConcurrentRunner._run`) called it with
only two arguments — so a CLI adapter always spawned in the process's CWD, not the
project, and `--sandbox` bind-mounted the wrong directory into the container
entirely. `AgentRunner` now takes `work_dir` at construction (it's a per-session
constant) and `assign`'s `working_dir` parameter is an override, not the only path
in. Separately, none of the three API adapters set a request timeout — a wedged
provider call never returned, and the session's collect timeout couldn't help: it
would stop *waiting*, then `close()` blocked *joining* that very call anyway. Each
adapter now accepts `timeout: float | None`, wired from the configured
`AgentConfig.timeout` via `bootstrap._api_factory`. The SDKs disagree on units —
Anthropic and OpenAI take seconds, `google-genai`'s `HttpOptions.timeout` is
**milliseconds** — so the Gemini adapter converts (`int(timeout * 1000)`); passing
seconds straight through would set a timeout 1000× too short and fail every real
call. `Session.close()` now calls `agent_runner.shutdown()` (duck-typed — the
injected `_Assigner` protocol doesn't declare it) so a pooled CLI subprocess no
longer outlives the session that spawned it, and on a wedged worker it shuts the
thread pool down with `cancel_futures=True` rather than joining — which drops
*queued* work but cannot interrupt a call already in flight; the per-request
timeout above is what bounds that one. Neither alone is sufficient; see the
`Session.close` docstring for the pairing.

**Three failure classes, not one (Wave 12).** `_assign_api` used to flatten
every `send`/`parse_result` exception into `f"api call failed: {exc}"` — which
blamed the transport for a truncated or malformed *response body*, and gave a
retry nothing to act on. It now distinguishes:

1. **`AgentResponseError`** (§1) — the provider answered, but the reply was
   rejected before or during decode (cut off, refused, undecodable). The
   failed `TaskResult` carries the exception's `stop_reason`, `usage`, and
   `retryable` straight through, so the session (§10) can log them and, for a
   refusal, stop retrying immediately.
2. **Any other exception out of `send`** — a genuine transport/SDK failure,
   still reported as `"api call failed: …"`.
3. **Any other exception out of `parse_result`** — a decode MAK did not
   anticipate; reported as `"could not decode agent result: …"`, not blamed on
   the transport.

### 7.6 Sandboxing CLI agents

CLI agents are arbitrary external processes — an attack surface. `sandbox.py`'s
`SandboxConfig.wrap(argv, working_dir)` builds the `docker run` argv that runs the
agent in a container with its filesystem scoped to the working directory (bind-mount
+ workdir) and its network restricted (`--network none` by default). The CLI
`--sandbox` flag threads a `SandboxConfig` into every CLI adapter (API adapters make
no subprocess and ignore it); `docker_available()` lets the CLI fail fast with
guidance if Docker is missing. The module only *builds* argv and probes the daemon,
so it is unit-testable without Docker.

### 7.7 Local transports: `local_api` and `ollama_api` (Wave 15)

Two more agent types reach a model running on this machine (or one the user names
by URL) instead of a hosted API — the shared design decisions live in `TASKS.md`
§15.0 (**D1–D13**); this is what they built.

- **`result_schema.py`** (a prerequisite refactor). Before this wave the
  `TaskResult` schema was written out twice, verbatim, differing only in how a
  nullable `error` is spelled (Anthropic's `input_schema`, Gemini's
  `parameters`). `result_schema(dialect)` renders the one contract — property
  names, descriptions, and the required set are module constants — in four
  dialects: `anthropic` (a `type` union for nullable), `gemini` (`nullable:
  true`), `openai` (**strict** JSON Schema — `additionalProperties: false` and
  *every* property in `required`, which is what OpenAI strict mode and
  vLLM/llama.cpp guided decoding demand), and `ollama` (plain JSON Schema for
  llama.cpp's grammar converter — no `type` unions, no `anyOf`, so `error` is
  a plain optional string). The Anthropic and Gemini adapters were refactored
  onto it as a **pure refactor** — their existing tests are the regression
  gate and pass unchanged, and the two rendered schemas are byte-identical to
  the literals they replaced.
- **`local_api`** — the *same* `OpenAiApiAdapter` class, registered under a
  second agent type with `base_url` required (D1). Why a second type rather
  than just adding `base_url` to `openai_api`: the `AdapterRegistry` is keyed
  by agent **type**, so with one shared type a roster could have cloud OpenAI
  **or** a local model, never both — and nothing downstream (the health
  preflight, the planner's agent-type list, `--models` parsing, warnings,
  logs, the TUI) could tell a local run from a cloud one. The constructor
  takes an `agent_type` kwarg (default `"openai_api"`) that the composition
  root overrides for a `local_api` instance, so it reports its own name
  everywhere the kernel asks.
- **The key is never leaked (D2).** With `base_url` set, `_get_client` sends
  the configured `api_key_env`'s value if one was named, and the literal
  placeholder `"local"` otherwise — and always sends *something*, so the SDK
  can never fall back to reading `OPENAI_API_KEY` from the environment and
  POSTing a real cloud key to whatever host the config names. This is the
  wave's one security property; it has a named test at both unit and
  acceptance level (`test_a_real_openai_key_in_the_environment_is_never_forwarded`),
  deliberately named so a future refactor cannot delete it silently.
- **The output-cap field name differs by transport (D4).** `max_completion_tokens`
  for cloud OpenAI (unchanged); `max_tokens` when `base_url` is set, because
  Ollama's and llama.cpp's OpenAI-compat layers implement only the older
  name — sending the newer one there is at best ignored, so the cap silently
  would not exist.
- **`structured_output` and the capability ladder (D5; revised by Waves 22 and
  24).** `AgentConfig.structured_output` is `auto`, `json_object`,
  `json_schema` (strict schema / constrained decoding — the `ollama_api`
  default, where it is native and free), or `none`. A call rejected for naming
  an unsupported response format descends **all the way**
  (`json_schema → json_object → none`), not once: a single downgrade made the
  bottom rung unreachable from the top, so a server supporting neither schema
  nor object mode failed every task. Anything that is not a verified format
  rejection propagates unchanged.

  The original "no adaptive memory across calls" rule was right about the
  hazard and wrong about the conclusion. The registry does rebuild adapters per
  dispatch, so memory cannot live on the instance — but the answer is an object
  the composition root **owns and injects**
  (`mak/endpoints/capabilities.py::CapabilityCache`), not a module-level dict.
  That is not the global mutable state `AGENTS.md` forbids: nothing is
  importable-and-mutable and two sessions in one process each get their own.
  Without it a forty-task run against a server with no structured-output
  support paid forty wasted requests to rediscover the same fact. Wave 24 adds
  the other half — the endpoint's *published* capabilities choose the starting
  rung, so the common case costs zero wasted requests rather than one.
- **The parse → repair → retry turn (D6), shared via `repair.py::repair_loop`.**
  A decode failure used to cost a full re-dispatch — the whole bundle (write
  sources, sibling context, caller context: tens of KB) sent again to
  re-earn an answer the model had already worked out and merely mis-shaped.
  For a small local model a malformed first reply is the common case, not the
  rare one, so the adapter instead sends **one short follow-up turn** carrying
  the model's own previous reply plus `protocol.py::REPAIR_INSTRUCTION`,
  bounded by `agents[].repair_attempts` (adapter default `1`; `0` disables
  it). Never after a truncation or a refusal — `repair_loop` runs
  `read_meta` (which raises those) *before* any payload is read, so neither
  repeats through a rung it cannot fix. Usage is **summed across turns**
  (`TaskResult.repairs` records the count, merged after the model's own keys
  so it cannot be forged) — `Session.total_tokens` and therefore
  `session.max_total_tokens` (§11) are computed from it, and a repair turn
  billing invisibly would put the only spend ceiling MAK has out by however
  many repairs a run needed. The OpenAI-compatible and native Ollama adapters
  (below) share this one driver so the two transports cannot repair
  differently.
- **A malformed body is a `protocol` failure, not an `api` one (D7).** *(A bug
  found while reading for this wave.)* `openai_api` used to raise a bare
  `AgentError` for "no choices" / "no content" / "not valid JSON" / "not an
  object" — `AgentError` is the *parent* of `AgentResponseError`, so
  `AgentRunner._assign_api`'s `except AgentResponseError` missed it, the
  result was classified `error_kind="api"`, and `Session._retry_note` (§10)
  emitted the generic "that produced nothing usable" note instead of the one
  that restates the schema. The schema-restating retry note — added
  precisely for a model that slips on shape — had never fired for the
  JSON-mode adapter. Now raises `AgentProtocolError`; same fix applied to the
  Anthropic/Gemini "no tool_use block" / "no function call" paths.
- **`health_check` probes a `base_url` endpoint once.** Unchanged when
  `base_url is None` (constructing the client is the whole check, and
  "building a registry performs no network call" stays true for cloud
  adapters). With `base_url` set, one `models.list()` call under a 5s
  timeout proves the server answers — "the server isn't running" used to
  surface as three failed dispatch attempts per task instead of one startup
  line. A failing probe sets `health_detail()`, a new optional method
  (`getattr`-checked, so no adapter is forced to implement it) that
  `bootstrap.healthy_agent_types` carries back so `mak/__main__.py`'s
  startup warning can name *why* — "Ollama is not running at
  http://localhost:11434" / "model 'qwen2.5-coder:14b' is not pulled" —
  instead of the generic "missing API key/SDK, or CLI not on PATH", which is
  never the reason a local server is unreachable.

`ollama_api` (`mak/agent_runner/adapters/ollama_api_adapter.py`) is a **native**
adapter over Ollama's own API rather than the OpenAI-compatible one, for one
reason the module docstring calls "the heart of D11": **Ollama's runtime context
defaults to a few thousand tokens regardless of what the model supports, and it
silently truncates an over-long prompt rather than erroring.** MAK's bundles run
to tens of KB, so the naive local setup hands a model a fraction of its task and
returns a confident, wrong, well-formed answer with nothing in any log to explain
it — the OpenAI-compatible path cannot fix this, because `num_ctx` is not an
OpenAI parameter. So the adapter:

1. reads the model's real context length from `/api/show` (cached on the
   instance — it does not change while a process runs);
2. sizes `options.num_ctx` to the bundle: `min(model_context_length,
   round_up(estimate_tokens(prompt) * 1.25 + num_predict))`, floored at 4096.
   The estimate is the documented `len(prompt) / 4` heuristic
   (`estimate_tokens`) — it does not have to be exact, only conservative, and
   the 1.25 margin is deliberate: over-estimating costs memory,
   under-estimating costs the silent truncation this module exists to
   prevent;
3. **refuses, loudly and non-retryably**, when the bundle cannot fit even the
   model's real window — the new `AgentContextExceededError`
   (`mak/core/exceptions.py`, an `AgentResponseError` subclass,
   `retryable = False`, `kind = "context"`) names the estimated prompt size,
   the model's limit, and the two settings that fix it
   (`session.dependency_context_bytes` / `session.cross_file_context_bytes`,
   or a larger model). Non-retryable because the same bundle re-sent is the
   same overflow. A configured `num_ctx` (unset = auto-size) is honoured
   verbatim and enforced as a hard ceiling instead — the user asked for
   exactly that window.

`format` is the constrained-decoding lever, and the reason `json_schema` is this
adapter's *default* structured-output mode (D5): Ollama compiles the schema to a
grammar, which on a small model is usually more reliable than cloud-style tool
calling — a grammar constrains syntax, not intent, so the system prompt still
describes the shape in words. `num_predict` is the resolved output budget,
explicit rather than Ollama's unlimited default, because a small model that
starts looping is otherwise bounded only by the timeout. `keep_alive` (e.g.
`"30m"`) keeps the model resident between tasks — without it every task can pay
a multi-second reload. Usage comes from `prompt_eval_count`/`eval_count`
(`stop_signals.py::_USAGE_FIELDS` gained those two names); `done_reason` goes
through the same shared `check_stop_reason(..., provider="ollama")`, and
`"length"` was already in `TRUNCATION_STOP_REASONS`. `health_check` checks two
independent things — the server is reachable (`version()`) **and** the
configured model is present (`list_models()`) — and `health_detail()`
distinguishes which one failed, which is where the `health_detail` seam above
earns its keep.

`mak/agent_runner/adapters/repair.py::repair_loop` is the one driver both local
adapters call through — parameterized by `call` (make one request), `read_meta`
(reject a cut/refusal before any payload is read; return usage + stop reason +
raw text), `extract` (pull the JSON payload from a response), and `follow_up`
(append the model's previous reply plus the repair instruction). The validation
decode inside the loop is thrown away and `parse_result` decodes the accepted
payload again downstream — cheap next to a model call, and it keeps
`parse_result` a pure function of the string the adapter returns.

`mak/planner/llm.py::OllamaPlannerLLM` and `build_planner_llm`'s backend
resolution are the planner's half of this wave — see §8.

## 8. Planner & human-in-the-loop review

`mak/planner/` is the only module that calls an LLM.

- **`planner.py`** — `Planner.decompose(user_task, node_inventory)` builds a prompt
  containing the task and the current node inventory (qualified names only, never
  source), calls an injected `PlannerLLM` (anything with
  `complete(prompt) -> str`), and validates the JSON plan with `parse_plan`. The
  parser accepts a bare array or `{"subtasks": …}`, strips code fences, validates
  each `SubTask` (including the optional `context_nodes`), and rejects duplicate ids
  and unknown dependencies. The prompt includes a **CASCADE PREVENTION** paragraph
  that instructs the model: if any sub-task changes a function's public signature
  (rename, add, remove, or reorder parameters; change defaults or return type), the
  plan must also include sub-tasks for every node that calls that function across all
  files. This minimises the need for post-wave cascade detection — it is better for
  the planner to address call sites upfront than to discover them as cascades after
  the first wave. Five further **target-node rules** keep a plan inside what the
  kernel can actually execute (each raises `ValueError`, so `decompose` retries with
  the reason fed back and the model self-corrects):
    - **Containment (Wave 17), checked first.** A target's file component must
      resolve *inside* the working directory — no absolute path, no `..`
      component, nothing under the mak dir (`mak/core/paths.py::
      unsafe_node_id_reason`, §2). This runs before the `.py` check below on
      purpose: containment is the more fundamental property, and an id like
      `/etc/cron.d/payload.py` satisfies the extension rule perfectly. The node
      id becomes a real filesystem path twice downstream — the node store's
      fragment dir, the reconstructed file under the work dir — and
      `Path(work_dir) / "/etc/x.py"` collapses to `/etc/x.py`, discarding the
      work dir entirely, so this is the first of three independent gates on the
      same property (the node store and `Session.install_plan` are the other
      two, §2 and §10 — `install_plan` needs its own because the interactive
      app and every cascade wave call it directly, bypassing `parse_plan`).
    - **Python-only targets.** Every `target_node`'s file component must end in
      `.py` (`is_python_target`). A `.md`/`.json`/`README`/doc target is rejected —
      MAK has no AST node for it, so it could never be ingested or reconstructed
      (this is what made an "architecture doc" task fail cryptically deep in the
      parser before the gate existed).
    - **One whole file, one task.** A whole-file target (a bare `path.py`, §2) must be
      owned by exactly one task; two tasks each returning the entire file would
      clobber each other and serialize on the one node. To split a file across tasks,
      target distinct symbols (`path.py::kind::name`). The prompt also steers the
      model to decompose a new project by file (one focused module per task).
    - **One granularity per file.** A file may not be targeted *both* as a whole file
      and by individual symbols in the same plan: the whole-file commit supersedes the
      file's fragments (§2), so a sibling fragment task would lose its work. Pick one —
      a single whole-file task, or only symbol tasks.
  On a malformed response it retries up to `max_retries`, feeding the rejection
  reason back, then raises `PlannerFailedError`. The retry loop covers the LLM
  *call* as well as the parse: a transient provider failure (rate limit, dropped
  connection) is retried with exponential backoff (1s, 2s, 4s, capped at 8s)
  instead of aborting the run with the budget untouched, while a `PlannerFailedError`
  from the backend (missing SDK, refusal) is re-raised immediately because retrying
  cannot fix it. A rejected *plan* is re-asked with no delay — waiting does not make
  a model answer better. Truncation gets its own feedback (see `response.py` below):
  the retry asks for a **smaller** plan rather than a corrected one.
- **`response.py`** — turns raw response text into JSON and, critically, tells
  **malformed** apart from **truncated**. `loads_json` strips a code fence found
  anywhere in the reply (not just at position 0), skips framing prose on both sides,
  and parses with `raw_decode` so trailing commentary does not fail a good response.
  When parsing fails, `repair_truncated` decides which kind of failure it was by
  closing the delimiters the payload left open — after discarding a trailing
  incomplete element — and checking whether the result parses; if it does, the
  response was cut short and `TruncatedResponseError` is raised carrying
  `complete_elements` (how many whole items the model emitted before the cut).
  The repaired text is **never** returned as a plan: it is a partial plan, and
  running one would edit half the codebase and report success. It exists to classify
  and to report, not to salvage.
- **`review.py`** — `display_plan_for_review` renders the subtask list and dependency
  edges and loops **approve / edit (paste corrected JSON) / abort**
  (`PlanReviewAborted`). I/O is injected (`prompt_fn` / `printer`) for testability;
  `--no-review` skips the call. An optional `header` parameter allows a prefix
  banner to be shown before the plan — the CASCADE WAVE review uses this to display
  `=== CASCADE WAVE ===` so the user knows they are reviewing a dynamically
  generated follow-on plan, not the original. A bad plan (a missed dependency or
  hallucinated edge) causes agent collisions or needless serialization that are
  expensive to unwind mid-session, so this ~5-second human check removes the single
  point of failure in one-shot LLM DAG generation.
- **`llm.py`** — concrete `PlannerLLM` completion backends (Anthropic / OpenAI /
  Gemini), each a thin prompt-in/text-out wrapper with a lazy SDK and injectable
  client (distinct from the agent adapters, which force a structured `TaskResult`).
  `build_planner_llm(model)` picks the backend from the model-id prefix, so the CLI
  can construct a working planner from `config.planner.model` alone.
  `resolve_max_tokens(model)` sizes the output budget from the model's own
  documented `max_output` in the model catalog (§13), clamped to 4,096–32,000, and
  falls back to 16,384 for a model the catalog does not know — as of Wave 12 this
  is a thin delegate over the shared `mak.core.budget.resolve_output_budget`
  (§7.2.1), which the agent adapters now use too with their own clamp, so both
  call sites share one catalog lookup instead of two. A fixed 4,096-token
  budget used to cut real plans off mid-string, and because the same request
  produces the same over-long plan, the cut repeated on every retry and failed the
  run — so each backend also reports a provider-signalled cut
  (Anthropic `stop_reason == "max_tokens"`, OpenAI `finish_reason == "length"`,
  Gemini `MAX_TOKENS`) as `TruncatedResponseError` before parsing is even attempted.
  An Anthropic `refusal` stop reason raises `PlannerFailedError` directly, since
  re-sending the same prompt earns the same refusal. The Anthropic backend
  **streams** (`messages.stream(...)` + `get_final_message()`): a plan-sized
  budget is past the point where the SDK will run a request non-streaming at all
  (*"Streaming is required for operations that may take longer than 10 minutes"*),
  because an idle non-streaming connection can be dropped before a long
  generation finishes. `get_final_message()` returns the same assembled message a
  non-streaming call would, so `stop_reason` handling is unchanged.
  **`OllamaPlannerLLM`** (Wave 15) is the fourth backend, over the native
  Ollama client (§14): `complete(prompt)` requests **no** `format` — the
  planner parses its own JSON through `loads_json` above, which already
  tolerates fences and prose, so constraining the reply to a grammar would
  mean maintaining a second schema for a shape the planner alone owns. It
  sizes `num_ctx` by the same rule the `ollama_api` agent adapter does
  (§7.7): a plan prompt lists the **whole node inventory**, so the D11
  problem applies here too, and an oversized inventory raises
  `PlannerFailedError` naming the numbers rather than letting Ollama
  truncate the inventory and plan for half a repo (open problem 1's real
  fix is shrinking the inventory itself — this wave only makes the failure
  loud instead of silent).
  **`build_planner_llm(model, *, backend=None, base_url=None, api_key=None,
  timeout=…)`** resolves the backend in three steps, in this order, because
  each is a stronger signal than the next: `backend` when set
  (`"anthropic"`/`"openai"`/`"gemini"`/`"ollama"`, via `planner.backend` in
  config or the TUI's `/local` wizard) always wins; otherwise `base_url`
  being set routes to the OpenAI-compatible backend (a `base_url` *is* a
  statement about the transport); otherwise today's model-id prefix routing
  runs unchanged. The first two steps exist for local models specifically —
  an id like `qwen2.5-coder:14b` or `llama3.1` matches no prefix, and without
  them a local planner would raise `PlannerFailedError` before a single call.
  `mak/__main__.py::_planner_api_key` now checks `config.planner.api_key_env`
  first (for a token-protected gateway, e.g. `vllm --api-key`), then falls
  through to today's provider inference, then `None` — `None` is the correct
  answer for a local planner, not a fallback: it is what lets
  `OpenAiPlannerLLM` apply the same D2 placeholder-key rule the agent adapter
  does (§7.7).
  **`warn_local_planner_mismatch(config)`** (beside `warn_model_caveats` in
  `mak/__main__.py`) warns on stderr when **every** configured agent is local
  but the planner is not: `--models ollama:qwen2.5-coder:14b` looks fully
  local and quietly is not — the run still ships the whole node inventory to
  a hosted provider to plan — which for an air-gapped user is the whole
  point of the wave, failing silently. Hybrid (a cloud planner beside local
  agents) is a legitimate, common configuration, so this is a warning, not
  an error, and it stays silent whenever the planner names a local backend
  or `base_url` itself.
- **`depgraph.py`** (Wave 10) — a static dependency-graph extractor, purpose-built to
  *validate* a plan rather than detect a live conflict (that job stays in
  `mak/conflict_detector/*`, which is untouched). `build_dep_graph(sources)` parses
  each `{node_id: source}` pair and produces a `DepGraph`: `references` (node →
  frozenset of nodes it calls or reads) and `definers` (short symbol name → defining
  node ids, methods keyed both as `name` and `Class.name`). Resolution is
  deliberately shallow and conservative — a same-file call resolves to a same-file
  definer; a cross-file reference resolves only through a parsed import table
  (`from a import b`, `import x.y`, relative imports, aliases) to a **uniquely**
  resolvable file; anything ambiguous (an unresolved import, `self.foo()`, a chained
  call) yields **no edge** rather than a guess. `dep_graph_from_store(node_store)` is
  the convenience wrapper the session actually calls, rebuilding the graph from the
  node store's current committed state on every `install_plan` (cheap — one
  `ast.parse` per node — and never cached across waves, since a commit invalidates
  it).
- **`validation.py`** (Wave 10) — `validate_plan(plan, graph, inventory)` cross-checks
  an LLM-produced plan against the `DepGraph` and returns a `ValidationResult` (a
  corrected copy of the plan — the input is never mutated — plus a list of
  `PlanFinding`s for HitL/logging). The auto-fix policy is deliberately asymmetric,
  because a false correction is worse in one direction than the other:
    - **Missing `depends_on` edges are auto-added.** If task A's target references a
      node task B writes, and B isn't already reachable from A, the edge is added —
      but only if it's acyclic-safe (checked via DFS over the accumulated edge set as
      each candidate is applied); a mutual pair that would cycle is reported as a
      finding instead ("mutual — consider merging or manual ordering") and left
      unapplied. Every addition is still surfaced as a finding so HitL shows exactly
      what changed and the user can strip it via the edit flow.
    - **Hallucinated node ids are auto-corrected only on a single confident match.**
      Grounding tries, in order: an exact id with the wrong `::kind::` segment; the
      same file/short name modulo case or underscores (`fooBar` ≈ `foo_bar`); a
      method missing its `Class.` prefix (a unique suffix match); then
      `difflib.get_close_matches` at a 0.8 cutoff, accepted only at ratio ≥ 0.9.
      Multiple or weak candidates become an `unknown_node` finding with suggestions
      and the plan is left as-is — an LLM decomposition targeting a genuinely new
      symbol or new file (no candidate at all) produces no finding, since that's a
      legitimate part of the planner's existing contract.
    - **Spurious `depends_on` edges are flagged, never removed** — a declared edge
      with no reference either direction between the two tasks' nodes becomes a
      `spurious_dep` finding, because the LLM may know a semantic ordering the AST
      can't see; validation suggests and corrects, it never silently discards intent.
    - **Unknown `context_nodes` are dropped** (with a `context_dropped` finding) —
      context is soft, so a phantom id is just removed rather than corrected —
      **unless another task in the same plan targets it** (Wave 13). Grounding runs
      in two passes for exactly this: targets first, then context against the
      inventory *plus every task's targets*. In a greenfield wave a task's context
      names modules its siblings are about to write, so they are absent from the
      inventory by construction, and deleting them left the reader with an empty
      bundle (one real plan dropped 14 context nodes this way). The fix is narrow:
      the fuzzy-match candidate list stays the real inventory, so a near-miss is
      never auto-corrected toward an id that does not exist yet, and a context node
      no task creates is still dropped.
    - **A forward context reference adds the ordering edge** (Wave 13) — if task A
      reads a context node that task B creates and A does not already (transitively)
      depend on B, `B → A` is added with a `missing_dep` finding; a mutual reference
      is reported and left alone, as elsewhere. Without the edge A can dispatch
      before the node exists and be starved anyway. `_add_missing_edges` cannot
      cover this: it works off the `DepGraph`, which is built from committed code
      and cannot see a file no one has written yet.
      The read-lock objection that originally justified dropping is settled, not
      re-litigated: `Scheduler._lock_requests` appends `(node_id, READ)` and the
      lock table never consults the node store, so locking a not-yet-written id is
      legal — and with the edge above, it has been committed by dispatch time.
    - A correction is reverted (downgraded back to a suggestion finding) if applying
      it would violate a `parse_plan` invariant, e.g. create a second whole-file
      owner for one file.
  `PlanFinding.kind` is one of `missing_dep` / `spurious_dep` / `unknown_node` /
  `corrected_node` / `context_dropped` / and, since Wave 20 (§5.2, `PlanSemantics`),
  `relaxed_dep` / `declared_api_dep` / `shared_structure` / `ordered_table` /
  `registry_key_collision`.
- **`contracts.py` (Wave 20).** `parse_contract(text)` accepts the natural
  spellings a planner writes (`"def f(a: int) -> R"`, with or without a
  trailing `:`/`...`) and normalizes to a canonical signature; `contract_stub`
  renders it as a parseable `def f(a: int) -> R: ...` for use as a signature
  authority before the implementation exists; `implementation_mismatch(text,
  source)` compares a committed source's actual signature to the declared one
  (name, parameters with annotations/defaults, return, async-ness, or a
  class's bases) and returns why they differ, or `None`. `_coerce_subtask`
  (`planner.py`) validates the four declaration fields at parse time — an
  `api_targets`/`contract`/`registry_keys` entry naming a node the task does
  not target, a contract that does not parse or names the wrong symbol, or
  `changes_api: false` alongside a contract, all raise `ValueError` so the
  retry loop feeds the reason back to the model rather than letting an
  unhonourable promise reach the kernel. `changes_api` left `null` while
  `api_targets`/`contract` are set is read as declaring `true` — naming an API
  target *is* declaring a change. `mak/semantic/contracts.py` is the
  session-side use of a parsed contract (which edges may soften, what a task
  should be shown) — see §5.2, §10.
- **Config-gated strategy and self-critique** (Wave 10, `Planner`, `planner.py`) —
  two opt-in refinements on top of the default one-shot `decompose`, both off by
  default so nothing about the default LLM call count changes:
    - `strategy="outline"` (`planner.strategy: outline`) runs two passes instead of
      one: a file-level **outline** call (steps naming which files they touch and
      their `depends_on` on each other, validated for uniqueness and acyclicity via
      a small Kahn check) followed by one **detail** call per outline step, each
      given only the node inventory restricted to that step's files. Detail-call
      task ids are namespaced `s<k>.<id>` and an outline edge `S1 → S2` adds every
      `S1` task id to every `S2` task's `depends_on` (correct-by-construction and
      deliberately conservative — the validation pass above is what surfaces
      resulting over-serialization to HitL). The merged result is re-run through
      `parse_plan`'s invariants exactly once before being returned.
    - `self_critique=True` (`planner.self_critique: true`) adds one reflection call
      after a plan is produced (either strategy): the plan is shown back to the
      model, which replies `{"verdict": "ok"}` or a corrected full plan in the same
      schema. A `verdict: ok` or anything that fails to parse **keeps the original
      plan** — a broken critique response must never break a good plan — and no
      retry budget is spent on it. Deterministic validation (above) always runs
      *after* critique, so the order in `Session.plan()` is decompose(+critique) →
      validate → review.

## 9. Git integration

`mak/git_integration/git.py` treats Git as an **audit log**, not an isolation layer
— lock discipline already prevents conflicting writes, so all commits go directly to
the working branch (no branches, no worktrees). `GitHelper`:

- `commit_task(task_id, files, description, agent_type, session_id)` commits exactly
  `files` with a `[MAK-<task_id>]` subject and a `Files/Status/Agent/Session` body,
  returning the commit hash — or `None` when those files are byte-identical to HEAD
  (an empty diff is a no-op, not an error, so a no-change reconstruction does not
  crash the session).

  **The user's index is never touched (Wave 19).** This used to run `git add` on the
  real index, check the *whole* index with `git diff --cached`, and then run an
  unrestricted `git commit`. Two things followed, neither of which an audit log is
  entitled to do: a user's staged `unrelated.txt` was swept into MAK's commit, and a
  task whose own files had not changed still committed whatever else was staged. The
  commit is now built in a **private index** (`GIT_INDEX_FILE`, a
  `.git/mak-index-<uuid>` seeded from HEAD with `read-tree`, or `read-tree --empty`
  on a repo with no HEAD yet), so the resulting tree is exactly "HEAD plus these
  files" regardless of what the user has staged. The emptiness check is
  `diff-index --cached --quiet HEAD --` against that private index. The temp index is
  unlinked in a `finally`, so a Git failure leaves the real index byte-identical to
  what it was. After a successful commit, `git update-index --add -- <files>`
  re-stats **only the committed paths** so `git status` does not report MAK's own
  commit back as staged modifications; a failure there is a warning, since the commit
  has already landed.

  **Policy for pre-existing edits to task-owned files.** What is committed is the
  working-tree content MAK materialized, which after startup reconciliation (§10)
  already incorporates any uncommitted edit the user had made to that file. A
  *partially staged* version of a task-owned file is therefore not what lands in the
  commit — the file on disk is — and the user's index entry for it is left alone.
- `get_session_commits(session_id)` parses `git log` into `CommitInfo` filtered by
  session; `push(branch, remote)` coordinates the single end-of-session push.
- `validate_clean_state()` checks porcelain. It had **no callers** until Wave 19;
  it is now what `git.require_clean_tree` enforces — an opt-in precondition (off by
  default) that refuses to start a session on a dirty tree, so `git diff` after a run
  means exactly "what MAK did". It is opt-in because that is a policy a project
  chooses: MAK's commits are path-scoped either way.
- `ensure_initialized()` (called from `Session.initialize` when `auto_commit` is on)
  guarantees the work-dir is its **own** repo before any commit. If the dir is nested
  inside an outer repo (a classic footgun: a project under a git-tracked home
  directory) or is in no repo at all, it runs `git init` there — and sets a *local*
  identity only when git cannot resolve one (never overriding a user's global
  identity). This keeps MAK's audit commits inside the project instead of leaking them
  into a surrounding repo.

All operations shell out to `git` and raise `GitIntegrationError` with stderr on
failure — nothing is swallowed.

## 10. Session lifecycle

`mak/session.py` wires everything together behind an explicit `SessionState`
machine: `CREATED → INITIALIZED → PLANNED → RUNNING → {COMPLETED | FAILED | ABORTED}`.

- **initialize** — prune, then ingest the working dir's Python files into the node
  store. **MAK's own `mak_dir` is skipped unconditionally**, independent of
  `exclude_patterns` (`_is_store_path`): the store persists fragments as `.py` files
  and `Path.glob("**/*.py")` descends into dotted directories, so before Wave 11
  every run re-ingested the previous run's output as project source — an exact,
  compounding `+325` nodes per run in the case that motivated the fix, leaving 89% of
  the inventory as garbage. `**/.mak/**` is also in the default excludes and the
  shipped `config.yaml`, but the unconditional skip is what survives a user
  overriding that list. `prune_excluded_nodes()` runs first and evicts stored nodes
  whose file is no longer ingestable — the migration for a store poisoned before the
  fix (deleting `.mak/` by hand is the blunt alternative). The count is reported on
  `SESSION_STARTED` as `pruned_nodes` and printed to stderr. The project's
  `.makignore` (§3.1.1) is loaded before the prune and honored by both the prune and
  the walk; if missing, it is created with `.mak/` and `.git/` after the clean-tree
  check. Exclusion pruning is a
  **migration sweep, not a deletion policy** — recording that a human deleted a
  symbol is `retire_node`'s job, below.

  **Startup is now four ordered steps (Wave 19): own, recover, clear, reconcile.**
  1. `_acquire_project()` takes the project's exclusive lease *first*, before
     anything reads or mutates `.mak/`. This is what makes step 3 sound: the old
     `lock_table.clear()` dropped a prior run's leases having established nothing
     about whether their owner was alive, so a second startup stripped a live
     session's locks. Holding the lease **is** the proof the prior owner is gone.
  2. `_recover_journal()` resolves any commit an interrupted run left in flight (§2),
     so reconciliation never runs against a mid-transaction working tree.
  3. `lock_table.clear()`, now provably safe.
  4. `_reconcile_work_dir()` — see below.

  **Reconciliation replaces one-directional ingestion.** `parse_file_into_nodes`
  used to return early whenever a whole-file node existed, *ignoring the source it
  was handed*; fragment re-ingestion wrote current fragments but never removed a
  symbol that had disappeared, and stamped everything version 1. Three observable
  consequences, all reproduced by the audit: a human's edit between two sessions was
  silently discarded, a deleted function kept reconstructing, and every node's edit
  history reset on each run. `NodeStore.sync_file` now diffs both directions —
  changed fragments advance to their *next* version, identical ones are untouched,
  and ids the new parse no longer contains are **retired**. A file the store knows
  and disk no longer has retires all of its nodes.

  A file whose content differs from the digest MAK recorded when it last wrote it
  (`file_state.json`) was edited by someone else. `session.on_external_edit` decides
  what happens: `"adopt"` (default) takes the working tree as the newer truth,
  `"conflict"` raises `WorkTreeConflictError` **during reconciliation**, before
  planning, so no agent can be handed content the tree no longer holds.
- **plan** — planner (+ optional self-critique) → deterministic validation → optional
  HitL review → `install_plan` (builds the DAG + persisted `Scheduler`).
  `install_plan` **always** re-validates the incoming plan — this is the one wire-in
  point that covers every path into it: `Session.plan()`, the CLI/TUI's direct
  `_planner.decompose()` + `install_plan()` call, cascade waves, and a user's edited
  plan from the review flow. Because it is the one wire-in point, it is also where
  `install_plan` runs its own containment check (`_reject_unsafe_targets`, Wave 17,
  §2/§8) *before* validation touches the plan — `parse_plan`'s equivalent check
  covers planner output, but two of the three ways a plan reaches the scheduler
  (the interactive app, every cascade wave) never call `parse_plan` at all, so
  `install_plan` needed its own gate rather than relying on the planner's. An
  escaping target raises `SessionError` naming every offender, not just the first.
  When `planner.validate` (default on), `install_plan`
  rebuilds the dependency graph from the node store's current committed state
  (`dep_graph_from_store`, §8) and runs `validate_plan` before normalizing agent
  types; the corrected plan is what actually gets installed, and
  `session.last_plan_findings` holds the findings (richer on the first pass inside
  `plan()`, since `install_plan`'s re-validation of an already-corrected plan is a
  cheap, mostly-empty second pass). One `PLAN_VALIDATED` event is logged with a
  per-kind finding count. `install_plan` also normalizes every task's `agent_type`
  (`_apply_default_agent`): a task with **no** `agent_type` is distributed
  **round-robin across the healthy agent pool** (so a multi-provider roster is
  actually used, not just the first agent); a task naming an **unconfigured/
  hallucinated** type is remapped to the pool's first entry (with a warning) rather
  than crashing dispatch with `UnknownAgentTypeError`; a valid type is left as-is.
  The pool is the healthy set from the startup preflight (§7.2); the planner prompt
  also lists the configured agent types so the model can pick one directly.
- **run** — dispatch lock-satisfiable ready tasks onto the thread pool (enriching
  each bundle with a layer 0 of declared contracts plus the write/read source via
  the four-layer enrichment in §3.2 — and, immediately after enrichment, capturing
  its read set, §5.2); as results arrive, **stage the source each agent returned**
  (`new_sources`, within the task's grant) via `put_node`; batch concurrently-
  completing results; run the Wave 20 commit pipeline (registrar reconciliation →
  read-set validation → the conflict detector → contract check → interface
  enforcement, §5.2) ahead of the parse/signature/import/collision checks it wraps;
  **transactionally** commit and reconstruct; write a git audit commit on success. A
  node the agent claims it changed but provides no source for cannot be committed,
  so a misbehaving agent fails its task cleanly rather than crashing the commit.
  During each commit, `_wave_committed` records `(old_source, new_source)` for every
  node committed this wave — and, since Wave 20, `(old_source, None)` for a fragment
  a whole-file commit **superseded**, so post-wave analysis sees a deletion the old
  code had no way to represent. `_wave_file_before` / `_wave_file_writers` /
  `_wave_node_writer` snapshot each touched file's pre-wave state and record which
  task(s) wrote it, and `_wave_fragments_before` / `_wave_commit_log` keep the
  per-commit fragment history the optional gates rebuild subset states from (§5.2).

  **Every attempt is diagnosable from the log alone.** `AGENT_RESULT` records what
  came back (task, attempt, success, granted ids, returned ids, per-id source
  length, error, and — Wave 12 — `no_changes_required`, `stop_reason`, `usage`);
  `SOURCE_DROPPED` records anything MAK refused to stage, with the id and the
  grant. And when a *successful* result leaves nothing to commit,
  `_describe_empty_result` names the actual cause instead of the old catch-all
  ("agent reported success but staged no usable source"), which described a symptom
  shared by several different causes: a `stop_reason` naming a provider truncation
  (checked first, Wave 12), ids outside the grant (listing both sides), ids
  listed with no source, success with no sources and a target that does not exist,
  success with no changes on a file that is still not valid Python, or — the
  remaining catch-all — success with no sources and no `no_changes_required`
  assertion, which is also exactly what a truncated reply looks like.
- **declared contracts, read sets, stale reads, registrars and parked commits
  (Wave 20)** — the prevention/detection/resolution machinery described in §5.2
  lives across `mak/semantic/*` and is wired into exactly the two points above:
  bundle enrichment (layer 0 + read-set capture, on the dispatching thread) and
  the commit pipeline (registrar merge → stale-read validation → contract check
  → interface enforcement, before the structural checks). A commit that cannot
  proceed **yet** — not wrong, just early — is **parked**
  (`_park`/`_resume_parked`/`_all_in_flight_parked`/`_release_parked_victim`)
  rather than sent back to the agent; every batch completion retries the parked
  set, and a tie among only-parked in-flight tasks is broken by releasing the
  highest-id one (re-gated on its dependencies via the new
  `Scheduler.wait_for_dependencies` if it was waiting on a contract provider,
  re-dispatched with a note otherwise — this is the one place a wait in this
  design can become a cycle, because parking is the only state where a task
  waits *while holding locks*). `_granted` records the lock **mode** each task
  was actually given per node (WRITE vs. the co-holdable INTENT_WRITE a
  registrar appender gets), so `_release_lock` and the commit-time
  `holds_all` re-validation release/check the mode a task really holds instead
  of assuming WRITE — the same reasoning that gave every lock request one
  shared builder in §4.5.
- **cascade detection** (`detect_cascade_tasks()`) — called after every `run()`
  wave, now assembling from **three** sources folded into one task per node
  (`_merge_fixups`, §5.2): cascade, cross-module defects, and the optional
  gates. **Cascade itself was rewritten onto the real reference graph in Wave
  20** — the AST-signature-diff-plus-regex approach described here before
  missed same-file callers, deleted symbols (there was no "new" signature to
  diff against), and anything a node id did not map onto one-to-one. It now
  diffs **symbols** across the wave (`mak/semantic/symbols.py::diff_symbols`)
  and walks the pre-wave **and** post-wave reference graphs for callers,
  skipping one whose calls are provably compatible with the new signature
  (§5.2 has the full mechanism). The CLI presents the result to the user as a
  **CASCADE WAVE** (via `display_plan_for_review` with a banner header); if
  approved, `install_plan` is called and `run()` executes another wave. The
  loop repeats until no cascades remain or the user declines. If the
  planner's CASCADE PREVENTION worked, this path fires zero times. Each
  generated fix-up task's id is a sanitized slug **plus a digest of the
  pre-sanitization subject** (`_fixup_task_id`, Wave 17): `[^a-zA-Z0-9]`
  sanitization is lossy — `a/b.py` and `a-b.py` both collapse to `a_b_py` —
  and `DAG` rejects a duplicate task id outright, so two unrelated files
  whose names happened to sanitize alike used to take down the *entire*
  cascade wave over a naming coincidence, not a real conflict.
- **cross-module defects** (`detect_cross_module_defects()`, Wave 13, extended
  Wave 20) — carried by the same call. A wave that creates two modules which
  disagree about each other's API changed no existing signature, so the
  cascade comparison above sees nothing, yet the code is broken exactly as if
  it had. Five checks run now, not one — the Wave 13 unresolved-import/arity
  check plus `attribute_check` / `override_check` / `constructor_check` /
  `cycle_check` / `duplicate_check` (§5.2) — all sharing one `ModuleIndex`
  (`mak/conflict_detector/module_index.py`) for import resolution and class
  lookup, and all run against the **pre-wave** state too so only a defect the
  wave introduced is reported. The session assembles the current (and, for
  the baseline, the pre-wave) source of every relevant file, scopes to files
  touched this wave, logs each defect as `CONFLICT_DETECTED`, and turns each
  offending file into an `api_fix_<file>` fix-up `SubTask` — now carrying a
  bounded diff of *both* sides and naming the task(s) behind each
  (`_pair_context`, R2) — appended to the cascade list, so every class of
  breakage shares one review flow. Both this and cascade cache their result
  per store `generation`, since the cascade loop asks twice for the same
  state.
- **optional heavy gates** (`mak/semantic/gates.py`, Wave 20, §5.2) — a fourth
  source of fix-up work, all off by default: a type-check diagnostic diff
  against a baseline taken at `initialize()` (`_take_gate_baseline`), impacted
  tests with pairwise attribution, and an import smoke test. None can fail a
  wave; a gate whose tool is missing is logged (`GATE_FINDING`) and skipped.
  An optional LLM adjudicator (`mak/semantic/adjudicator.py`) is installed per
  wave (`_install_adjudicator`, re-budgeted each `install_plan`) and consulted
  only from inside stale-read validation for a case the static checks
  couldn't settle — it is not a fifth gate, it never generates a fix-up task
  on its own.
- **the cascade loop itself** lives in `mak/cascade.py` (Wave 16), not in a front
  end. `run_cascade_waves(session, approve, announce=...)` drives detect → announce
  → approve → install → run until nothing remains, the approver declines, or
  `max_waves` bounds a self-feeding loop. Both `mak/__main__.py` and `cli/app.py`
  call it with their own presentation and approval; neither owns *when* a fix-up
  wave runs. It was in one front end before: the CLI ran the guard and the
  interactive app did not, so whether a defect the kernel could name got reported
  depended on which entry point the operator happened to launch.

  **It returns a `CascadeOutcome`, never `None` (Wave 19).** It used to return only
  its *last* `SessionResult`, and both front ends then did
  `if cascade_result is not None: result = cascade_result` — replacing the original.
  Combined with `install_plan` resetting the per-wave accumulators, an initial wave
  with a failed task plus a successful fix-up wave presented as a clean success:
  green tally, zero failures, exit code 0, push armed. The two ways the loop can stop
  without finishing had no representation at all — reaching `max_waves` returned the
  last successful result with nothing marking the limit, and a declined wave returned
  whatever happened to be there. `CascadeOutcome` carries every wave plus `declined`,
  `limit_reached`, and `unresolved` (filled by one final detection pass after the
  loop — the difference between "we are done" and "we stopped").
- **the aggregate** is `ExecutionResult` (`mak/execution_result.py`): the initial
  wave plus the cascade outcome, reported as one thing. It answers two questions
  *separately*, which is the whole point — `tasks_completed` is a **statistic**, and
  `request_satisfied` is the **verdict** that gates exit codes and the push. Four
  tasks completing across two waves is not the same as the user getting what they
  asked for. **An earlier failure is never cleared by later work**: a cascade wave is
  new work about the callers a *successful* change broke, so it has no standing to
  resolve a failed task, and the aggregate will not infer that it does. Task ids are
  namespaced by wave index so two waves that both produce `fix-1` stay
  distinguishable.
- **teardown** — run the project's test suite and push if green (when `auto_push`).
  The suite is the `session.test_command` (e.g. `pytest -q`) run in the work dir by a
  `TestRunner` built in the composition root (`mak/test_runner.py`); it reports a real
  `(passed, output)`.

  **Teardown returns a `TeardownResult`, not a bool (Wave 19).** The bool started at
  `True` and only moved if a runner existed, so a project with no `test_command`
  logged `tests_passed=True` and — with `auto_push` on — *pushed*, every run. It also
  never looked at the run itself, so failed, blocked, and skipped tasks pushed too,
  and in the TUI a teardown that raised was printed as a warning while the flag
  stayed `True`. There are four outcomes now (`mak/teardown.py`): `passed`, `failed`,
  `skipped` (nothing ran), and `error` (the runner raised). The push gate requires
  `git.auto_push`, a git helper, a **satisfied aggregate execution outcome**, and
  `session.test_policy` — `require_pass` (default) opens the gate only for a suite
  that genuinely passed; `allow_skip` is the opt-out for a project with no suite.
  `TeardownResult.push_skipped_reason` names whichever gate refused.

Robustness properties worth knowing:

- **Transactional commit (rewritten in Wave 19)** — the prospective file is
  reconstructed and `compile()`-validated *before* any `commit_node`, and the
  commit that follows is a real transaction rather than a best-effort revert.

  What the old wording ("a post-commit write failure triggers a best-effort
  revert, so the node store and disk never diverge") promised, the code did not
  deliver, in four distinct ways — all found by the 2026-09-08 audit while the
  full suite was green. `_reconstruct_affected` wrote each file in turn with
  `Path.write_text`, so a two-file change could write the first and fail on the
  second; `write_text` truncates before writing, so an interruption *destroyed*
  the file rather than skipping it; `revert_node` rolls back to `version - 1`,
  which a brand-new node does not have, so a failed first-version commit stayed;
  and `commit_node` deleted a superseded file's fragment directories before
  reconstruction succeeded, destroying the history a rollback needed.

  The shape now is: `NodeStore.transaction()` snapshots the in-memory index and
  **defers** its three destructive effects (the metadata save, superseded-fragment
  removal, version pruning); `install_files` renders *every* affected file in
  memory, journals each destination's prior content, then writes them all with
  `write_text_atomic`. **The store's metadata save is the commit point.** Before
  it nothing durable has changed and the index is restored verbatim; after it the
  deferred deletions drain and the change is recoverable in full. `Session._revert`
  is gone — the transaction is the rollback.

  Proven by `tests/test_wave19_acceptance.py` (criteria 1–3) and
  `tests/node_store/test_sync.py`, each of which asserts the invariant against a
  **freshly reopened** store, because agreement between live in-memory objects is
  exactly what the defect already had.
- **Crash recovery of an in-flight commit (Wave 19)** — the journal
  (`mak/node_store/journal.py`) is written before the first output file is touched
  and carries a backup of every destination. A later process reads it and decides
  by comparing the versions it recorded against the versions the reopened store
  holds: all matching means the commit point passed, so it rolls *forward*; any
  differing means it did not, so it rolls *back*. A journal in the `installed`
  phase means files and store are both durable and only the Git audit is in doubt,
  which recovery resolves by re-running `commit_task` — a retry Git itself makes
  idempotent by reporting an empty diff.
- **Partial completion** — when a result's `modified_nodes ⊊ target_nodes`, the
  completed grants are accepted and committed and only their locks released; the
  *remaining* grants are re-dispatched as a narrowed task. This is tracked per task
  by `SubTaskProgress` and bounded by `max_attempts`.
- **No-op acceptance requires the agent's assertion, not just an absence (Wave
  12).** An "audit / review" task may legitimately inspect an already-complete
  file and report there is nothing to change — but `success=True` with no
  fragments used to be accepted as exactly that, and it is also byte-identical
  to what a reply cut off at the provider's output cap looks like. A real run
  hit this: two tasks (`marks`, `modes`) were **silently marked complete**
  having received no work, because their target files happened to already
  exist from an earlier run — while two others (`motions`, `search`) failed
  outright on the identical empty-result shape, purely because *their* files
  didn't exist yet. The run reported `tasks_completed: 4.0`; real progress was
  2 tasks out of 20.

  The fix (`Session._is_asserted_noop`): a no-op is accepted only when the
  agent *set* `TaskResult.no_changes_required` — a field a truncated reply can
  never contain, since the model never got far enough to write it — **and**
  the existing guards still hold: the targets already exist (`_target_exists`)
  and `_file_is_syntactically_valid()` confirms the assembled file passes
  `compile()`. `Session._accept_noop` then closes those grants, syncs the
  committed node store content to disk (`_reconstruct_affected`, unchanged from
  before — ensures a prior run's whole-file node that never reached
  reconstruction is corrected before teardown's tests run), and logs
  `ACCEPTED_NOOP` (§1) — kept apart from an ordinary `TASK_COMPLETED` so the
  log shows "decided there was none" apart from "did the work". This is
  distinct from the misbehaving-agent case (success that *claims* edits but
  stages no source), which is not accepted; from a create task whose target
  does *not* exist and returns nothing, which correctly still fails; and now
  from an **unasserted** empty success, which also correctly fails — the
  contract the old code lacked.

  **The remaining gap, and its narrowing (Wave 18).** Even hardened, that guard
  was an *existence* check, not a *work* check: an agent that found a task hard
  could still close it by setting one boolean, as long as the target file
  happened to be there and to parse. `Session._noop_refusal` adds the two cases
  where the assertion cannot be true whatever the agent believes — both read off
  **the wave's own plan**, never off the agent's answer, which is what makes them
  checkable at all. `install_plan` (and `recover`) snapshot the files that had
  committed nodes when the wave was installed; a target outside that snapshot is
  refused when either (a) a task this one directly `depends_on` targets the same
  file — MAK's own edge asserting the dependency is what created it, so there was
  nothing to inspect when the plan was written — or (b) it is a **whole-file**
  grant on the **first** attempt, the same argument without the edge. Only the
  first attempt, because a second has seen the retry note and the file's real
  contents, so its assertion is about something. Direct edges only: a transitive
  ancestor's output has been visible for at least one commit.

  A refused grant stays open and goes through the ordinary retry/fail path, and
  the refusal text is written to be the retry instruction — `_describe_empty_result`
  returns it verbatim and in preference to its own generic tail, which would
  otherwise tell an agent that *did* assert a no-op that it had not. Everything
  outside those two cases keeps the acceptance path it has always had; this is a
  narrowing, not a redesign.
- **Crash recovery** — `recover()` expires stale leases and rebuilds the scheduler
  from `task_graph.json` via `from_persisted` (which also restores each task's
  `context_nodes`, so recovered tasks re-acquire their read locks). It is reachable
  from the CLI: `mak run --recover` calls `recover()` instead of `initialize()`/
  `plan()` and resumes the persisted plan (so a fresh run's `initialize()` — which
  clears the lock table — never destroys the crash state). `--task` is optional when
  `--recover` is set; if there is no `task_graph.json` to resume, the CLI exits
  non-zero with a message. **A corrupt graph degrades the same way (Wave 17):**
  `Scheduler.from_persisted` raises `SchedulingError` on a graph it cannot parse —
  the exact failure mode a kill mid-write produces — and `recover()` catches it,
  logs `SESSION_ENDED(recover_failed=True, …)`, prints the reason, and leaves the
  session un-planned rather than propagating. Before this, `--recover` broke on
  precisely the crash it exists to handle; now the operator sees "nothing to
  resume" and starts a fresh run instead of an unhandled traceback. `lock_table.json`
  and `NodeStore`'s `metadata.json` get analogous per-store policies — a lost lock
  table just starts empty (every lease in it is reconstructible), and a corrupt
  metadata index is quarantined to `metadata.json.corrupt` with the fragments left
  untouched (§2) — because what a corrupt file costs differs per store, and only
  the task graph's cost is "nothing to resume."
- **Honest stall reporting** — a run is `COMPLETED` *only if* the scheduler is
  genuinely done; otherwise `SessionResult` splits the strays so the outcome is
  legible: `failed` (a task that exhausted `max_attempts`), `skipped` (a task with a
  **failed ancestor** — an expected downstream consequence, computed by walking the
  dependency edges to a fixpoint), and `blocked` (stranded for some *other* reason —
  an unsatisfiable DAG or a lock that never freed, with no failed ancestor). A run
  with any of the three reports `FAILED`, never a false success.
- **Diagnosable failures** — each task's most recent failure reason is captured
  (`_failure_reasons`): the agent's `TaskResult.error` (an API error or a
  truncated/malformed structured reply), or the rejection reason from
  conflict/parse/lock-revalidation, or a fallback for an agent that claimed success
  but staged nothing. `SessionResult.failure_reasons` carries it for the failed
  tasks, and the CLI prints one line per failed task — so a failure is never a bare
  task id with no explanation. **Every distinct reason is reported, not just the
  last** (`_record_failure` / `_final_failure_reason`): a task can fail differently
  on each attempt, and reporting only the final one buries the cause — a real run
  was rejected twice by the same underlying defect and then hit a one-off malformed
  response on its third attempt, so the run reported *only* the malformed response,
  which named nothing relevant. One recurring reason still reports as one plain
  sentence; two or more are listed in the order first seen.
- **A retry differs from the attempt it follows, and a refusal doesn't burn the
  budget (Wave 12).** `_handle_incomplete` used to re-queue a task's remaining
  grants unchanged — for a truncation, that is three identical API calls
  producing three identical cuts, since the same request is cut at the same
  point every time (this was directly observable in one session's log:
  `motions` and `search` each failed on three byte-for-byte identical
  `agent_result` events). It now:
    - computes a `retry_note` (`Session._retry_note`) and stashes it on
      `SubTaskProgress`, so `_submit_partials` attaches it to the re-dispatched
      `TaskBundle` (§1, §7.4). What the note says is chosen by *how* the attempt
      failed (`TaskResult.error_kind`, §1), because that is what decides what a
      different answer would look like. A truncation gets a **compaction**
      instruction ("return the same work in less output… if one node's full
      source genuinely cannot fit, return `success=false`" rather than a partial
      rewrite). A **schema slip** (`error_kind == "protocol"`) gets the schema
      restated in full — the generic note said the previous answer was unusable
      but never what shape was wanted, so one run returned `modified_fragments`
      as a string three times running. Any other failure gets the recorded reason
      plus an instruction not to repeat it;
    - fails the task **immediately**, without spending the remaining attempt
      budget, when the result is marked `retryable=False` (a refusal) — the
      same prompt would earn the same refusal on every remaining attempt, so
      `_handle_incomplete` treats an unretryable result the same as an
      exhausted attempt count, and the reason names why ("not retryable — the
      remaining N attempt(s) would repeat it verbatim").
- **The spend ceiling** (Wave 18) — `_run_loop` checks `_budget_breach()` at the
  top of every iteration, before `tick()`. On a breach it calls
  `_stop_on_budget`: log a `SESSION_ENDED` carrying `budget_exhausted`, the
  ceiling and the spend; `_finish_in_flight` collects and processes the results
  of everything already dispatched, so work in progress commits normally; then
  the loop breaks. `_finish_in_flight` is bounded by the in-flight *count* rather
  than by `scheduler.dispatched` emptying, because a partially-completed task
  re-queues itself for a narrower re-dispatch this loop deliberately never makes
  — waiting for the set to drain would wait forever. `_finalize` forces `FAILED`
  and reports `SessionResult.stopped_reason`, which both front ends print, so
  the stranded tasks are explained rather than showing up as an unattributed
  "3 blocked". The gate is checked *between* iterations and never inside
  `_process_one`: a run that has overspent stops dispatching, it does not abandon
  a commit half-applied. See §11 for the config knob and why `total_tokens` is
  the number compared.
- **Plan-quality metrics** (Wave 10) — `_run_loop` samples `len(scheduler.dispatched)`
  after every `tick()` into `_concurrency_samples`; `_reject` increments
  `_conflict_rejections` and `_handle_incomplete` increments `_redispatches` on a
  partial-completion re-dispatch. `_finalize` computes `max_concurrency`,
  `mean_concurrency` (2 dp), `conflict_rejections`, `redispatches`,
  `tasks_completed`, `tasks_failed`, and (Wave 12) `tasks_noop` into
  `SessionResult.metrics` (an additive dict field, default `{}`, so every prior
  `SessionResult(...)` construction and equality check stays valid) and logs one
  `PLAN_METRICS` event — a per-wave signal for whether a plan's `depends_on`
  structure is actually realizing parallelism or serializing/colliding more than
  it should. `tasks_noop` (`Session._noop_task_ids`) counts a completed task only
  when *every* grant it closed was an asserted no-op (a task that changed one
  node and declined another still counts as work done) — so `tasks_completed`
  is never inflated by hollow completions, and `SessionResult.noop` carries the
  task ids for the CLI/TUI to print apart from the headline "N completed" (both
  `mak/__main__.py` and `cli/ui.py::show_results` now render
  `"N completed (M no-op)"` and, in the CLI, list which tasks were no-ops).
  Wave 13 adds the *input* side of the same accounting: `dispatches`,
  `context_bytes_total`, `mean_context_bytes`, and `starved_dispatches`, summed
  across every attempt from the `TASK_DISPATCHED` path (§3.2). A wave whose mean
  context is near zero produced its results without being shown the code, which is
  worth knowing before trusting them.
  Wave 20 adds `stale_reads` and `stale_redispatches` — every stale read found
  during commit validation (§5.2), and how many of them a re-dispatch was
  needed for; the difference between the two is how many the kernel settled
  on its own (accept, or a re-verified shape-only change) without spending an
  agent call at all.
- **Token accounting is the session's own, not scraped from an SDK (Wave 17).**
  `Session.token_usage` / `Session.total_tokens` sum what each provider actually
  reported on its own response: `_agent_usage` accumulates `TaskResult.usage` as
  results are processed, and `Planner.token_usage` (recorded on `PlannerLLM` after
  every `complete()` call, including retries and the optional critique pass) is
  folded in. This replaced three SDK monkeypatches in `cli/runner.py` that hooked
  `Messages.create` / `Completions.create` / `Models.generate_content` — wrong as
  well as fragile, since the Anthropic agent adapter and planner both call
  `messages.stream`, which never routes through `Messages.create`, so the old
  counter reported a flat zero for MAK's default provider. `cli/runner.py`'s
  `session_tokens(session)` is now a thin read of `session.total_tokens`; nothing
  patches a vendor SDK internal any more (§12.2).

All collaborators are injected behind `Protocol`s, so the session is testable with
fakes.

## 11. Configuration

`mak/config.py` loads and validates `mak/config.yaml` into a `MakConfig` dataclass
tree (all frozen, `slots`). The schema:

```yaml
session:
  work_dir: "."
  mak_dir: ".mak"
  max_concurrent_agents: 3      # used by Wave 5's thread pool
  lock_timeout_s: 300.0
  deadlock_check_interval_s: 5.0
  test_command: "pytest -q"     # run in the work dir at teardown; gates auto_push
  dependency_context_bytes: 24000  # per-bundle budget for the source a task
                                   # carries from the tasks it depends on (Wave
                                   # 13, §3.2); past it entries degrade to an API
                                   # digest. 0 disables the layer, -1 unbounded
  cross_file_context_bytes: 32000  # per-bundle budget for the cross-file caller
                                   # layer (Wave 16, §3.2); past it entries are
                                   # dropped, not digested. Same 0 / -1 semantics
  # max_total_tokens: 2000000      # spend ceiling for one run (Wave 18): input +
                                   # output, every agent call plus the planner's.
                                   # Unset (the default) is unbounded
  on_external_edit: "adopt"        # Wave 19, §10 — what startup reconciliation does
                                   # with a file edited since MAK last wrote it.
                                   # "adopt" takes the working tree as the newer
                                   # truth; "conflict" raises before planning
  test_policy: "require_pass"      # Wave 19, §10 — whether a push may happen when
                                   # no suite ran. "require_pass" says only a suite
                                   # that ran and passed opens the gate;
                                   # "allow_skip" is the opt-out for no suite

# Endpoints (Wave 22, "Universal OpenAI-compatible endpoints" in the history
# section below) — an optional list of named OpenAI-compatible services. An
# agent or the planner names one by id instead of repeating its URL and
# capabilities. `mak/endpoints/profiles.py` is the single source of truth for
# every preset URL and key-env name; nothing below is duplicated anywhere else
# in the tree (a test enforces this).
# endpoints:
#   - id: "nvidia"
#     profile: "nvidia"        # nvidia | openrouter | deepseek | zai-general |
#                               # zai-coding | custom — prefills base_url,
#                               # api_key_env and capability defaults; every
#                               # field it sets can still be overridden here
#     location: "hosted"       # hosted | private | local — only affects what
#                               # MAK tells you, never how it connects
#   - id: "house-gateway"      # a service with no built-in profile
#     transport: "openai_chat"
#     base_url: "https://llm.internal.example/v1"  # the SDK base, never
#                                                    # rewritten with a "/v1"
#     api_key_env: "HOUSE_GATEWAY_KEY"  # the NAME of the env var; omit for a
#                                        # keyless server — MAK sends a
#                                        # non-secret placeholder, never an
#                                        # ambient OPENAI_API_KEY
#     model_discovery: "auto"   # auto | models | manual
#     health_check: "models"    # models | chat | none
#     structured_output: "auto" # auto | json_schema | json_object | none —
#                                # starts strict and steps down on a verified
#                                # rejection, remembering what worked
#     token_parameter: "auto"   # auto | max_tokens | max_completion_tokens | none
#     provider_routing: "none"  # none | openrouter (Wave 24) — whether this
#                                # endpoint's body may carry a provider-routing
#                                # extension. Only the `openrouter` preset sets
#                                # `openrouter`, and it is never inferred from a
#                                # hostname: a proxied or renamed endpoint would
#                                # then be guessed wrong in both directions.
#     headers:                  # extra request headers; MAK owns Authorization,
#                                # Content-Type, Host and User-Agent and refuses
#                                # an entry that tries to set one
#       - name: "X-Title"
#         value: "MAK"          # a literal, public value
#       - name: "X-Tenant-Token"
#         value_env: "HOUSE_TENANT_TOKEN"  # a secret — read from the environment

planner:
  model: "claude-opus-5"
  max_retries: 3
  validate: true          # cross-check the plan against the code dependency graph —
                          # ground node ids, add missing depends_on edges (Wave 10)
  strategy: "oneshot"     # "oneshot" or "outline" (outline -> per-step detail)
  self_critique: false    # one extra LLM reflection pass over the produced plan
  # backend: "ollama"       # Wave 15, §8 — explicit planner backend, when the
                            # model id's prefix cannot say (a local model id
                            # matches none): anthropic | openai | gemini | ollama
  # base_url: "http://localhost:11434"   # required for an "ollama"/local backend
  # api_key_env: "VLLM_TOKEN"            # for a token-protected gateway; unset
                                         # means the D2 placeholder key is sent
  # endpoint: "nvidia"        # Wave 22 — route the planner through a configured
                              # endpoint instead. Authoritative when set: MAK
                              # does not guess the backend from the model name,
                              # and the credential comes from the endpoint's own
                              # api_key_env, never OPENAI_API_KEY. Mutually
                              # exclusive with backend/base_url/api_key_env above

agents:                         # first entry is the default agent
  - type: "anthropic_api"
    model: "claude-sonnet-5"
    api_key_env: "ANTHROPIC_API_KEY"
    max_instances: 2
    timeout: 300
    # max_tokens: 32000       # output budget override (Wave 12) — unset resolves
                              # from the model catalog for anthropic_api, and
                              # sends no cap (inherits the model's own maximum)
                              # for openai_api/gemini_api; see §7.2.1
  - type: "openai_api"
    model: "gpt-5.6-sol"
    api_key_env: "OPENAI_API_KEY"
  - type: "gemini_api"
    model: "gemini-3.5-flash"
    api_key_env: "GEMINI_API_KEY"
  # Wave 15 (§7.7, §14) — a local runtime, or any OpenAI-compatible server:
  # - type: "ollama_api"
  #   model: "qwen2.5-coder:14b"
  #   base_url: "http://localhost:11434"   # optional; this is the default
  #   structured_output: "json_schema"     # json_object | json_schema | none
  #   repair_attempts: 1                   # 0 disables the repair turn
  #   num_ctx: 32768                       # unset = auto-sized per bundle
  #   keep_alive: "30m"
  #   temperature: 0.1
  # - type: "local_api"                    # vLLM / LM Studio / llama.cpp / …
  #   model: "Qwen/Qwen2.5-Coder-32B-Instruct"
  #   base_url: "http://localhost:8000/v1" # required — MAK never guesses a port
  #   structured_output: "json_schema"
  # Wave 22 — route through a configured endpoint instead of type/base_url:
  # - id: "nvidia-llama"        # the routing key: scheduler, planner, logs and
  #                             # the git trailer all use it, not the type
  #   endpoint: "nvidia"        # type is derived from the endpoint's transport
  #   model: "meta/llama-3.3-70b-instruct"
  #   max_instances: 2
  #   timeout: 600
  # - id: "nvidia-qwen"         # a second model on the SAME endpoint — this is
  #                             # exactly what pre-Wave-22 could not express
  #   endpoint: "nvidia"
  #   model: "qwen/qwen2.5-coder-32b-instruct"

git:
  auto_commit: true
  auto_push: false        # gated on the *aggregate* outcome plus test_policy (§10)
  commit_prefix: "[MAK]"
  # require_clean_tree: false  # Wave 19, §9 — opt-in precondition: refuse to start
                               # on a dirty tree, so `git diff` after a run means
                               # exactly "what MAK did". Off by default

models:
  auto_refresh: true      # background-refresh the provider model catalog (§13);
                          # never touches planner.model or agents[].model

node_store:
  include_patterns: ["**/*.py"]
  # version_retention: 5          # on-disk versions kept per node (Wave 18, §2);
                                  # floor 2 (revert needs a prior version),
                                  # -1 keeps every version forever
  exclude_patterns:               # keep "**/.mak/**" — see below
    - "**/.mak/**"
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/__pycache__/**"
    - "**/.git/**"
    - "**/build/**"
    - "**/dist/**"
    - "**/.tox/**"
    - "**/.mypy_cache/**"
    - "**/.pytest_cache/**"
    - "**/site-packages/**"

# Semantic conflicts (Wave 20, §5.2) — every setting is optional; the values
# below are the defaults. All four locking flags default *on*; every gate
# defaults *off*.
# semantic:
#   stale_read: "revalidate"    # accept_if_api_stable | revalidate | redispatch | reject
#   api_locks: true             # split each node's lock into #api and body (§4.5)
#   intention_locks: true       # INTENT_WRITE on a fragment's file/class (§4.5)
#   registry_keys: true         # key-level locks + commutative append merge (§4.5)
#   contract_dispatch: false    # dispatch a dependent against a fully-declared
                                # contract instead of waiting for the implementation
#   type_check: "off"           # off | pyright | mypy — diagnostic diff gate (D3)
#   impact_tests: "off"         # off | on — pairwise-attributed impacted tests (D4)
#   import_smoke: "off"         # off | on — import every touched module fresh (D6)
#   adjudicator: "off"          # off | "<backend>:<model>" — LLM tie-breaker (D7)
#   adjudicator_max_calls: 5
#   gate_timeout_s: 300
#   impact_max_overlays: 12     # bounds how many subset states D4 may build
```

Rules and behaviors:
- `agents` is **required** and must be non-empty; each entry needs a `type`. Per-agent
  fields `model`, `api_key_env`, `cmd`, and (Wave 12) `max_tokens` are all optional.
  `max_tokens` must be a positive integer if set — `0`, a negative value, or a
  non-numeric string all raise `ConfigError` at load time rather than silently
  clipping every reply to nothing. `None` (unset) is not the same as passing an
  explicit value: it is omitted entirely when the composition root constructs the
  adapter (`_api_factory`, §7.3), so the adapter's own default — resolve from the
  catalog, or send no cap — is the single place that decides the budget.
- **`config.yaml` is the single source of truth for model choice.**
  `PlannerConfig.model` defaults to `""` (unset) in `mak/config.py` — the dataclass
  carries no hardcoded model name, mirroring `AgentConfig.model` (`str | None = None`).
  A missing `planner.model` key is not silently backfilled with a name the user never
  chose; it comes from `config.yaml` (the bundled default names one) or, for the
  interactive CLI, from whatever `/planner` last wrote back to it. The CLI only
  touches `config.yaml` when the user explicitly changes a model.
- `planner.validate`/`strategy`/`self_critique` are parsed by `_parse_planner`
  (Wave 10, §8): `strategy` must be `"oneshot"` or `"outline"` or `_PLANNER_STRATEGIES`
  raises `ConfigError`; the two booleans go through `_as_bool`. All three default to
  the safe, cheapest behavior (validate on, oneshot, no critique) so an existing
  `config.yaml` without them is unaffected.
- `models.auto_refresh` (Wave 14, §13) gates the background provider-catalog refresh;
  parsed by `_parse_models` via `_as_bool`, defaults `true`. This is the only config
  knob the model catalog subsystem reads — it never touches `planner.model` or any
  `agents[].model`.
- **API keys are never stored in config** — `api_key_env` names the environment
  variable to read at composition time. Put real keys in `~/.config/mak/.env`
  (written by the TUI's `/apikey` setup) or, in a source checkout, the legacy
  `mak/.env` (gitignored); both are auto-loaded at startup (`load_env_file`, §12)
  and `mak/.env.example` lists the expected variable names. Exported environment
  variables take precedence.

  Two hygiene fixes in Wave 18. `save_keys` creates `~/.config/mak/.env` with
  `os.open(..., O_WRONLY|O_CREAT|O_TRUNC, 0o600)` instead of writing it at the
  process umask and `chmod`-ing afterwards — the old order left the file at `0644`
  on a default account, with the keys already in it, for the window between the
  two calls. The `chmod` stays, now as a repair for a file an older MAK left
  behind. And the legacy `mak/.env` is **deprecated**: it lives inside the package
  directory, nothing enforces its mode (the working copy that prompted the audit
  was `0644` with live keys), and it only survives an upgrade by accident. It is
  still read for one release, both `load_keys` and `load_env_file` warn and name
  `~/.config/mak/.env` when they use it, and the next release drops it. It is
  gitignored and excluded from `package-data`, so it never shipped in a wheel.
  `tests/conftest.py` points both lookups at an empty temp dir for every test, so
  a developer's real keys can no longer change what the suite asserts.
- **`session.max_total_tokens` is the only cost ceiling there is (Wave 18).**
  `max_attempts` (3) × `max_iterations` (1000) × `max_waves` (10) × unbounded
  per-agent output multiply out to no bound at all; the per-wave cascade approval
  prompt is a human gate, not a budget. Parsed by `_opt_positive_int`, so `0` or a
  negative value raises `ConfigError` rather than silently disabling every
  dispatch. The number compared is `Session.total_tokens` — agents **and** planner,
  read off what each provider reported on its own response — which is the same
  figure the TUI counter and the final report show, so the three cannot disagree.
  The check sits between run-loop iterations, never inside result processing: a
  run that has overspent stops *dispatching*, lets what is already in flight
  finish and commit (`_finish_in_flight`), and reports `SessionResult.stopped_reason`
  naming the budget. It never interrupts a commit, so the working tree is never
  left half-written. Re-queued partial redispatches are dropped and surface as
  stranded tasks, which is what they are.
- **`node_store.version_retention` bounds the store on disk (Wave 18, §2).**
  Parsed by `_parse_node_store`; must be `-1` (unbounded) or at least `2`, because
  `revert_node` needs one prior version — anything else raises `ConfigError`
  rather than producing a store that cannot roll back. `mak gc` applies the same
  policy to a store an older MAK wrote (§12).
- **Config discovery** — when `--config` is omitted, `discover_config_path()`
  picks the first of: `./mak.yaml`, `~/.config/mak/config.yaml` (respects
  `$XDG_CONFIG_HOME`), then the packaged default `mak/config.yaml`. This is what
  lets an installed MAK (`uv tool install` / `pipx`) run without a checkout.
- **Model caveats** — `model_caveat(model_id)` returns a warning string for
  models that work with MAK but carry footguns (currently `claude-fable-5`:
  30-day data-retention requirement, `refusal` stop reasons, premium pricing).
  Every surface where a model is chosen prints it: the TUI's `/models`,
  `/planner`, and setup wizard, and `mak run` (`warn_model_caveats` in
  `mak/__main__.py`, once per distinct caveat on stderr).
- **`session.mak_dir` is anchored to `work_dir`, not to the process CWD (Wave
  17).** A relative `mak_dir` (the default `".mak"`) used to be interpreted
  against wherever the operator happened to launch `mak` from — `mak run
  --work-dir ~/projA` and `mak run --work-dir ~/projB` invoked from the same
  shell shared `./.mak/node_store`, and since node ids are work-dir-relative,
  `toolkit/registry.py` in project A and project B were literally the *same*
  id: re-ingestion was skipped once a whole-file node existed (Wave 19 replaced
  that with synchronization, §2), so B silently inherited A's content and
  reconstruction wrote it to disk. The TUI already
  anchored `mak_dir` correctly (§12.2); `mak run` did not. Both now call the
  shared `config.anchor_mak_dir(config)` — a relative `mak_dir` resolves
  against `work_dir`, an absolute one (an explicit override) is left alone —
  and `Session._mak_roots` collapsed from two speculative roots (CWD-relative
  *and* work-dir-relative, "hope one of them is right") to the one the config
  now unambiguously names. `config.stale_mak_dir(config)` detects a leftover
  `.mak` from before this fix at the old CWD-relative location and reports it
  on stderr; it is **never adopted** — deciding an orphaned store belongs to
  *this* project means guessing, and guessing wrong reintroduces the exact
  cross-project contamination the fix removes. `mak/__main__.py::main` calls
  `stale_mak_dir` *before* `anchor_mak_dir` — once the config is anchored,
  the old CWD-relative location is simply the answer to a question nothing
  asks any more, so the orphan check has to run on the pre-anchor config
  to see it at all.
- **`node_store.exclude_patterns` is a convenience, not the safety net.** Setting it
  *replaces* the defaults, so a config that omits `**/.mak/**` no longer excludes
  MAK's own store by pattern — which is exactly why `Session.initialize` skips the
  configured `mak_dir` unconditionally as well (§10). Everything else on the list
  (`.git`, `build`, `dist`, `.tox`, `.mypy_cache`, `.pytest_cache`, `site-packages`,
  `node_modules`, `.venv`, `__pycache__`) is an ordinary convenience default: paths
  that are generated, vendored, or not project source. Note that a path removed from
  ingestion is also *pruned* from the store on the next `initialize`.
- **Per-project ignores belong in `.makignore`, not in `exclude_patterns` (§3.1.1).**
  It adds to the config list rather than replacing it, lives with the project, and
  uses gitignore syntax, so ignoring one directory does not mean re-listing every
  default.
- Type coercion is strict and wrapped in `ConfigError` (e.g. `"false"` parses to
  `False`, not Python's truthy `bool("false")`).
- **The nine local-transport fields (Wave 15, §7.7, §14).** Six on `AgentConfig` —
  `base_url`, `structured_output`, `repair_attempts`, `num_ctx`, `keep_alive`,
  `temperature` — and three on `PlannerConfig` — `backend`, `base_url`,
  `api_key_env`. Every one is `None` when unset, the same rule `max_tokens`
  states above: the field exists so a value can be set, but the *adapter*
  stays the single place that owns the default. `structured_output` and
  `backend` are validated **at load** against a fixed set of choices
  (`_as_choice`) — a typo must fail before a run starts, not surface as a
  provider 400 mid-wave. `base_url` goes through `normalize_base_url` (also
  used by `agents_from_specs`, §12.1, and the TUI's `/local url`, §12.2) —
  `http://`/`https://` and a host are required, and a trailing slash is
  stripped, so a URL typed on the command line, one written in YAML, and one
  entered interactively are all validated by exactly one rule. `repair_attempts`
  accepts `0` (its own helper, `_opt_non_negative_int`, distinct from
  `_opt_positive_int` because `0` is meaningful here — it switches the repair
  turn off). `validate_config` (§7.3) rejects any of these six set on a type
  that ignores them, and rejects `local_api` **without** a `base_url` — MAK
  never guesses a port for a local server, so the message names Ollama's
  OpenAI-compat default (`http://localhost:11434/v1`) as the likely fix.
- **`semantic:` (Wave 20, §5.2, §4.5).** Parsed by `_parse_semantic` into
  `SemanticConfig`. `stale_read` and `type_check` are each validated against a
  fixed enum at load time (`_require_choice` / a direct membership check), so
  a typo fails before a run starts rather than acting as `revalidate`/`off`
  silently. `impact_tests` and `import_smoke` accept a YAML boolean or the
  literal strings `"on"`/`"off"` (`_on_off`) — the gates read as feature
  switches, not booleans, in the file. `adjudicator` is `"off"` (or unset) for
  none, otherwise `"<backend>:<model>"` with the backend checked against the
  same four planner backends (`_parse_adjudicator`); `Session._configured_adjudicator_llm`
  builds it through `build_planner_llm` (§8) exactly like a `PlannerLLM`, so a
  local adjudicator (`ollama:qwen2.5-coder:14b`) needs no extra plumbing.
  `adjudicator_max_calls` and `impact_max_overlays` must not be negative and
  `gate_timeout_s` must be positive, or `ConfigError` is raised. Every
  locking flag (`api_locks`/`intention_locks`/`registry_keys`) and
  `contract_dispatch` is a plain `_as_bool`. Every gate is **off** by default
  because none of them are free — a subprocess per touched module, several
  pytest runs, a model call — and every locking flag is **on** by default
  because none of them cost anything the pre-Wave-20 lock model did not
  already pay for. `Session(gate_runner=..., adjudicator_llm=...)` accepts
  both the gate subprocess runner and the adjudicator's `PlannerLLM` as
  constructor overrides, which is how the whole subsystem is testable with
  neither a real tool on `PATH` nor a real API key (`tests/semantic/`).

## 12. Command-line interface

`mak/__main__.py` is the entry point: `python -m mak --task "..."`. It is a thin
shell over the composition root, split into testable functions:

- `load_env_file(path=None)` — loads `~/.config/mak/.env`, then the legacy
  package-relative `mak/.env` (`KEY=VALUE` lines) into `os.environ` via
  `setdefault`, so the **documented key convention actually takes effect** and an
  explicitly `export`ed variable still wins. Called first in `main`; the
  agent/planner adapters then read keys from the environment at composition time.
  No `python-dotenv` dependency — it's a dozen lines. Since Wave 18 the legacy
  path warns on stderr when it supplies anything (§11).
- `parse_args(argv)` — flags: `--task` (optional; **required unless `--recover`**,
  enforced in `main`), `--config` (default: auto-discover via `discover_config_path()`,
  §11), `--work-dir`, `--models` (roster override, see below), `--max-agents`
  (concurrency override), `--agent` (override the default agent type), `--no-review`,
  `--recover` (resume a crashed session from `task_graph.json`, §10), `--sandbox`,
  `-v/-vv`.
- `build_session(args, config, sandbox)` — assembles the `Session` and all its
  collaborators (node store, lock table, registry via `build_registry`, agent runner
  with per-agent `timeout`/`max_instances` wired in, planner via `build_planner_llm`
  seeded with the healthy agent types, git helper, logger, the `test_command`
  `TestRunner`, the default agent, and the healthy `agent_pool`). It runs the startup
  **health preflight** here (§7.2).
- `main(argv, *, session_builder=build_session)` — loads env + config, applies the CLI
  overrides (work-dir, roster, concurrency), validates, **anchors `mak_dir` under
  `work_dir` and warns on stderr if a stale pre-anchor `.mak` sits next to the
  shell** (`anchor_mak_dir`/`stale_mak_dir`, Wave 17, §11), builds the session, drives
  **initialize → plan → run → cascade loop → teardown**, and maps domain errors to
  friendly messages and exit codes: `0` success, `1` for an aborted review / planner
  failure / failed-or-blocked run / failing tests, `2` for a config error (including
  a bad `--models` provider or `--max-agents < 1`) or a missing Docker daemon under
  `--sandbox`. The `session_builder` seam lets tests drive `main` end-to-end with a
  fully-faked session. The end-of-run summary counts `completed / failed / skipped /
  blocked` (§10) and prints each failed task's reason and the `skipped`/`blocked`
  lists separately, so a cascade from one root failure reads as one failure plus its
  dependents, not many independent problems.

  **Cascade loop** (between `run()` and `teardown()`): after each wave `main` calls
  `session.detect_cascade_tasks()`. If any are returned, it prints a warning and
  (unless `--no-review` is set) calls `display_plan_for_review` with a
  `=== CASCADE WAVE ===` header. If the user approves, `session.install_plan` and
  `session.run()` execute the next wave. Under `--no-review`, cascades are skipped
  with a stderr warning that callers may be broken. The loop repeats until no cascades
  remain, the user declines, or `--no-review` skips it.

### 12.1 Choosing agents and concurrency from the command line

Everything the roster needs can be set at the command line — **no config edit
required** — because `main` rewrites the loaded `MakConfig` (a frozen dataclass) with
`dataclasses.replace` before validation:

- **`--models PROVIDER[:MODEL] …`** overrides the config's entire `agents` list.
  `bootstrap.agents_from_specs(specs)` parses each entry: the part before `:` is a
  friendly **provider** name, the part after (optional) is an explicit **model**.
  `bootstrap._PROVIDER_TO_API` maps the provider to its adapter `type` and conventional
  key env var:

  | Provider (CLI) | Adapter `type` | Key env var | Default model (adapter) |
  |---|---|---|---|
  | `anthropic` | `anthropic_api` | `ANTHROPIC_API_KEY` | `claude-sonnet-5` |
  | `openai` | `openai_api` | `OPENAI_API_KEY` | `gpt-5.6-sol` |
  | `gemini` (alias `google`) | `gemini_api` | `GEMINI_API_KEY` | `gemini-3.5-flash` |
  | `ollama` (Wave 15, §7.7) | `ollama_api` | *(none)* | required |
  | `local` (Wave 15, §7.7) | `local_api` | *(none)* | required |

  With no `:model`, `AgentConfig.model` is left `None` and the adapter's built-in
  default applies — except `ollama`/`local`, where a local runtime has no
  catalog default and a missing model is a `ConfigError` showing the syntax. The
  first entry becomes the routing default (overridable with `--agent`).

  **A configured endpoint id is also accepted in the provider position (Wave
  22).** `--models nvidia:meta/llama-3.3-70b-instruct` resolves `nvidia` against
  the endpoints in `mak.yaml` plus the per-user endpoint store (`/endpoint add`,
  §12.2) and is tried *before* the five built-in provider names above; a
  reserved id (`anthropic`, `openai`, `gemini`, `google`, `local`, `ollama`) can
  never be taken by a user endpoint, so a prefix has exactly one meaning. Because
  the registry is keyed by **agent id**, not adapter type (§7.3), several models
  on one endpoint and several endpoints on one transport are both legal in a
  single roster — `--models nvidia:meta/llama-3.3-70b-instruct
  nvidia:qwen/qwen2.5-coder-32b-instruct openrouter:some/model` runs all three at
  once. This is the restriction Wave 22 removes: before it, the registry was
  keyed by adapter type, so a second entry naming the same provider (or a second
  OpenAI-compatible endpoint, which had no identity of its own) silently
  replaced the first in the registry rather than running alongside it. A
  collision is still rejected, but on the **agent id** two specs resolve to, not
  on the provider — `--max-agents` is still how you get several *instances* of
  one model running concurrently.

  **The full grammar is `provider[:model][@base_url]`.** `bootstrap._split_spec`
  splits on the **first** `@` (a URL may carry a userinfo segment,
  `http://user:pass@host/v1`, and `rpartition` would cut inside it; a model id
  never contains one) and then the **first** `:` (an Ollama tag itself contains
  one, `qwen2.5-coder:14b`, and partitioning on the first keeps it intact).
  `@base_url` is accepted on `openai` (a gateway or proxy), `local`, and
  `ollama`; naming it on `anthropic`/`gemini` is a `ConfigError` — neither has
  a notion of an alternate endpoint, and accepting the flag there would
  silently ignore it. `ollama` defaults to `http://localhost:11434` (falling
  back to `$MAK_LOCAL_BASE_URL` first) because the provider name *is* the
  runtime; `local` has **no default** — guessing Ollama's port for someone
  running vLLM is worse than asking, so a missing endpoint for `local` is a
  `ConfigError` naming the syntax. `SUPPORTED_PROVIDERS` is now these five
  names (`bootstrap.SUPPORTED_PROVIDERS`); an unknown provider's error message
  lists the set including the two local ones.

  ```bash
  --models ollama:qwen2.5-coder:14b                       # default endpoint
  --models ollama:qwen2.5-coder:14b@http://gpu-box:11434   # explicit endpoint
  --models local:my-model@http://localhost:8000/v1         # vLLM, LM Studio, …
  --models openai:gpt-5.6-sol@https://my-gateway/v1         # a gateway/proxy
  ```

  Neither `ollama` nor `local` needs an API key — the `_LOCAL_TYPES` set
  (§7.7) is exactly the two types the health preflight and the planner-mismatch
  warning (§8) also key off of. `OPENAI_API_KEY` in the environment is never
  forwarded to a `local:`/`openai:…@url` endpoint (D2, §7.7) — sent instead is
  the `api_key_env` value if one was configured, or the literal placeholder
  `"local"`.

- **`--max-agents N`** overrides `session.max_concurrent_agents` — the size of the
  bounded worker pool in `Session` (§10), i.e. **how many agents run at once**. This
  governs live concurrency. `AgentConfig.max_instances` is a *different* knob: it caps
  each agent type's retained **idle CLI-subprocess pool** in `AgentRunner` (so a long
  session doesn't accumulate idle processes); it does not bound live concurrency.
  `N < 1` is a `ConfigError`.

- **Planner key fallback.** The planner has its own model (`planner.model`, default a
  Claude model). `_planner_api_key` first looks for that provider's `api_key_env`
  among the roster, then falls back to `bootstrap.DEFAULT_KEY_ENV` — so an
  OpenAI-only `--models openai` run still resolves the Claude planner's
  `ANTHROPIC_API_KEY` from the environment.

Examples:

```bash
# Three providers, explicit models, default concurrency from config:
python -m mak --task "..." --work-dir ./proj \
  --models anthropic:claude-opus-5 openai:gpt-5.6-sol gemini:gemini-3.5-flash

# One provider, five concurrent workers:
python -m mak --task "..." --work-dir ./proj --models anthropic --max-agents 5
```

To make a roster permanent instead, edit the `agents:` list in `mak/config.yaml`; the
flags simply override it for a single run. Agent and planner backends are otherwise
selected entirely by the config file.

### 12.1.1 Using a local CLI agent (`claude_code` / `codex` / `copilot`)

The `--models` flag only builds the five API-transport providers (§12.1) — three
hosted, two local (Wave 15). To run a **local CLI** agent instead — the
`claude`, `codex`, or `gh copilot` you already have installed — add it to the
config's `agents:` list by `type` (there is no `--models` shorthand for CLI
agents):

```yaml
# ~/.config/mak/config.yaml (or ./mak.yaml, or mak/config.yaml)
agents:
  - type: claude_code       # drives your local `claude` via the bridge wrapper
    max_instances: 2
    timeout: 300
  # - type: codex           # drives `codex`
  # - type: copilot         # drives `gh copilot`
```

Then run normally: `mak run --task "..." --work-dir ./proj`. What happens:

1. **Prerequisite:** the underlying binary must be on your `PATH`. The startup
   health preflight runs the wrapper's `--health-check` (which is just
   `shutil.which("claude")` etc.); if it's missing, that agent is dropped with a
   warning, and the run aborts if it was the default.
2. On dispatch, the adapter launches `python -m mak.agent_runner.wrappers.claude_code`,
   which hands the CLI a prompt containing each target node's current source and asks
   for a JSON object mapping node id → rewritten source, then returns a `TaskResult`.
3. **Override the invocation** if your CLI needs different flags, without editing
   MAK: set `MAK_CLAUDE_CODE_CMD` (or `MAK_CODEX_CMD` / `MAK_COPILOT_CMD`), e.g.
   `export MAK_CLAUDE_CODE_CMD="claude -p --model claude-opus-4-8"`. The config
   `cmd:` field selects just the binary (`--cli <binary>`); the env var replaces the
   whole command line.

CLI agents are the **secondary** path — the API adapters are more robust because they
force structured output. Caveat: `gh copilot` is oriented toward shell-command
suggestions, so it's the weakest fit for MAK's node-rewrite protocol.

### 12.1.2 `mak update` and `mak gc` (`cli/__main__.py`)

`cli/__main__.py` is the `mak` console script: it dispatches the bare TUI, `run`
(forwarding to `mak.__main__.main`), `gc`, `update`, `examples`, and
`--version`/`--help`.

**`mak update` installs a release tag, not `HEAD` (Wave 18).** It used to run
`uv tool install git+https://github.com/…` with no tag, no pin, and no signature
check, so **any** push to `main` was auto-adopted by every user who ran `update` —
including a half-finished branch merge — and nothing told them what they were
moving to. `_resolve_update_target()` now resolves the newest release tag via
`git ls-remote --tags`, installs `git+<url>@<tag>`, and prints the version before
installing. Annotated tags appear twice in `ls-remote` output (`refs/tags/x` and
the peeled `refs/tags/x^{}`); the peeled entry wins, because it names the commit an
install of that tag actually builds from, which is what the existing PEP 610
`direct_url.json` comparison checks against. Ordering is a deliberately tolerant
`_version_key` rather than a PEP 440 parser — `packaging` is not a dependency and
the only tags it must order are this project's — with a plain release sorting above
any pre-release of the same number. When the repo publishes no tags at all it falls
back to `HEAD` **and says so** in the label, which is the honest answer for a
project that has not cut a release yet. The `ls-remote` pre-check that skips a
reinstall when already current is unchanged; it now compares against the tag.

**`mak gc [work_dir]`** discovers the config the same way a run does, anchors
`mak_dir`, and calls `NodeStore.gc()` (§2), reporting how many stale version files
and orphaned fragment directories it removed. A store written by a current MAK
stays bounded on its own — every commit prunes its own node — so this is the
one-time sweep for stores an older version left behind.

**`mak examples [name]`** (Wave 15, §14) lists — or, given a name, prints to
stdout — one of the four configs packaged under `mak/examples/`
(`local-ollama`, `local-openai-compatible`, `hybrid-cloud-planner-local-agents`,
`fully-local-offline`). `mak examples local-ollama > mak.yaml` is the whole
non-interactive quickstart for a local run. `config.example_path(name)`
resolves the name to a file **inside** `mak/examples/` and rejects anything
that resolves elsewhere — the name reaches this function from the command
line, and joining it to a package path unchecked is how "print my config"
becomes an arbitrary file read. An unknown name exits `1` listing what
exists; `tests/test_example_configs.py` loads and `validate_config`s every
packaged example, which is what stops a doc example rotting silently past a
schema change.

## 12.2 Interactive CLI app (`cli/`)

`cli/` is a Claude Code-style **interactive shell** for MAK — an inline REPL built
on `prompt_toolkit` + `rich`. It wraps MAK as a library (calling
`session.initialize()`, `session._planner.decompose()`, `session.install_plan()`,
`session.run()` directly, not via subprocess) so the full structured plan is
available in-process and the spinner/progress bar stays in the normal scroll region.

It runs the **same post-wave cascade loop** the command line does (`mak.cascade`,
§10), between `run()` and `teardown()`, presenting fix-up tasks with the same
`show_plan` + y/N prompt it uses for the first plan and honouring `/no-review`. Until
Wave 16 it ran no cascade detection at all, so a wave launched from the app could
leave a caller broken — or two new modules disagreeing about each other's API — with
nothing said about it.

### Starting the app

```bash
python -m cli
```

### UX design

The layout is benchmarked against Claude Code / Codex CLI: one accent color
(purple `#bd93f9`, the `ACCENT` constant in `cli/ui.py`), everything else default
or dim, flat indented lists instead of nested panels, and no startup command
dumps.

- **Welcome box** — a single compact `ROUNDED` panel on startup: name, version
  (from `mak/_version.py`), tagline, `/help` / `/status` pointers, and cwd.
- **Bottom toolbar** — live session state (models, planner, agents, workdir,
  approval, session tokens) rendered under the prompt on every keystroke via
  `PromptSession(bottom_toolbar=...)`. Because state is always visible there,
  slash commands print only a one-line `✓`/`⚠`/`✗` confirmation.
- **Slash menu** — typing `/` immediately pops the completion menu listing every
  command with a one-line description (`complete_while_typing=True` +
  `MakCompleter`); typing filters, Tab/Enter completes. Inline auto-suggest from
  history. Ctrl+J inserts a newline for multi-line tasks.
- **Task flow** — on any non-slash input: (1) capture the pre-task HEAD hash,
  (2) reset the token counter, (3) build + initialize a MAK session, (4) plan with
  a spinner, (5) show the plan as a flat wave list (accent `●` bullets with inline
  `target · agent · after` metadata), (6) optionally wait for human approval,
  (7) run agents with a `rich.progress` bar, (8) show results, (9) show a per-file
  git-stat-style diff of every change since the pre-task hash.
- **Git diff** — `get_git_diff(work_dir, pre_hash)` diffs `{pre_hash}..HEAD`,
  covering all commits MAK made during the task (not just the last one). Displayed
  as `+N -N` bars per file under a dim `changes` label.
- **Token counting** — `cli/runner.py::session_tokens(session)` reads
  `Session.total_tokens` after each run and accumulates it into `self._session_tokens`.
  On Ctrl+C/EOF the exit message shows `Session ended.  N,NNN tokens used.` **This
  replaced three SDK monkeypatches (Wave 17)** that hooked `anthropic
  …Messages.create`, `openai …Completions.create`, and `google.genai
  …Models.generate_content` at the class level. The patches were wrong, not just
  fragile: MAK's Anthropic agent adapter and its Anthropic planner backend both call
  `messages.stream` (a large output budget forces streaming — the SDK rejects a
  non-streaming call above ~10 minutes), which never routes through `Messages.create`
  at all — so the counter reported a flat **zero** for the default provider, and the
  old test suite only ever exercised the pure per-provider helpers (`anthropic_tokens`
  etc.), never asserted that the patched method was the one MAK actually calls. The
  session now sums what each provider reported on its own response
  (`TaskResult.usage`, plus the planner's own `token_usage`, §10), which is correct
  for a streamed call exactly like a non-streamed one and needs no SDK internals at
  all.

### Slash commands

| Command | Description |
|---|---|
| `/models [provider:model …]` | Select agent models (same `provider:model` spec as `--models`); in local/hybrid mode, bare `/models` lists the runtime's models live instead of the cloud catalog |
| `/planner [model]` | Switch the planner model; accepts a local model with no catalog lookup and no key check |
| `/refresh-models` | Re-fetch the *cloud* model catalog now, ignoring the refresh schedule (§13) — says so explicitly in local mode, where `/local models` is the equivalent |
| `/local [sub-command]` | Local-runtime setup — see below (Wave 15) |
| `/mode [cloud\|local\|hybrid]` | Show or switch how this session gets its models (Wave 15) |
| `/endpoint [sub-command]` | Add, edit, test, or remove a custom OpenAI-compatible endpoint — see below (Wave 22) |
| `/max-agents N` | Set the concurrent-agents limit |
| `/work-dir <path>` | Set MAK's working directory |
| `/apikey` | Add or update API keys interactively |
| `/config [path]` | Load a custom config file; bare `/config` returns to auto-discovery (§11) |
| `/no-review [true\|false]` | Toggle human approval before task execution |
| `/status` | Print the full session settings |
| `/help` | List commands and keyboard shortcuts |
| `/clear` | Clear the screen and reprint the welcome box |
| `/exit`, `/quit` | Quit MAK (Ctrl+C / Ctrl+D also work) |

### Mode, `/local`, and `/mode` (Wave 15)

**The problem this closes.** Before Wave 15, `MakCli.run` exited `1` when no
provider key was set and `run_setup` refused to continue with none — so a
fully-offline machine could never reach the prompt at all, regardless of
whether a local runtime was sitting right there. `CliState.mode` is now a
first-class field (`"cloud"` | `"local"` | `"hybrid"`, `cli/core/state.py`)
that decides which surfaces validate against API keys and which against a
local runtime, shown in the bottom toolbar and `/status` so a user can never
be unsure whether the next task costs money. It never changes *how the
kernel is configured* — the roster the runner builds is always
`selected_models`, filled with `ollama:qwen2.5-coder:14b@http://localhost:11434`
in local mode exactly as `--models` would take it, and `_apply_state_to_config`
("Session-only configuration" below) parses both through the same
`agents_from_specs`.

**First run** (`cli/setup.py::run_setup`) now asks the question before
prompting for anything else — after a **background** `discover()` scan so the
three options can be honest about what is actually on the machine:

```
  Welcome to MAK — how do you want to run models?

    1) Cloud     hosted APIs (Anthropic, OpenAI, Google)     ● 0 keys set
    2) Local     on this machine, private and offline        ● Ollama detected (3 models)
    3) Hybrid    cloud planner + local agents                — recommended for small local models

  Select (1–3):
```

**Cloud** runs the unchanged key wizard. **Local** hands straight to the
`/local` wizard below — no key is asked for, and choosing it with nothing
detected prints install guidance rather than failing (`MakCli` still reaches
the prompt). **Hybrid** runs the key wizard restricted to a planner key, then
the `/local` wizard for agents.

**`/local`** (`cli/local.py`) is the setup wizard, and bare `/local` runs it
end to end: **detect** (`mak.local.discover`, §14, with a spinner) → **choose
the runtime** if more than one answered (Ollama first) → **choose an agent
model** (installed models are listed with parameter size and quantization; if
none are installed, the curated suggestions from `mak.local.recommended`
appear instead, smallest first, and the chosen one is pulled with a `rich`
progress bar) → **choose the planner** (the same local model, a different
local model, or a cloud planner — recommended in one line, off the curated
table's `is_small()`, never off a size heuristic computed in the wizard) →
**report the context fit** (the chosen model's window beside MAK's current
`dependency_context_bytes` + `cross_file_context_bytes` — D11's footgun made
visible *before* the first run rather than after a bad one) → **confirm**,
which sets `state.mode`/`local_*`/`selected_models`/`planner_*`, then asks
`Save this setup to ./mak.yaml? [y/N]` — **default no**. This is not a special
case of MAK's "never writes config except on an explicit model change" rule
("Session-only configuration" below); it *is* that rule, applied to a setup
wizard instead of `/models`. Saving renders one of the packaged examples (§12.1.2) with the
chosen values substituted and round-trips it through `load_config` +
`validate_config` before writing, so the file a user ends up with is the
documented one.

Every `/local` sub-command survives an unreachable server: an `OllamaError`
becomes one red line naming the endpoint, never a traceback, never a crash of
the prompt loop.

| `/local` sub-command | Behaviour |
|---|---|
| `status` | endpoint, version, models installed, models currently loaded (`/api/ps`) |
| `models` | list what the runtime offers, live |
| `use <model> […]` | set the agent model(s) |
| `planner <model>` | set the planner to a local model |
| `pull <model>` | download a model with a progress bar (interruptible; resumes on re-run) |
| `url <base_url>` | point at a custom endpoint (validated by `normalize_base_url`, then probed) |
| `off` | drop back to cloud mode (keeps the API keys already set) |

**`/mode`** with no argument lists the three modes and what each needs; with
one, it switches — refusing with an actionable message ("no local runtime
configured — run `/local`" / "no API key set — run `/apikey`") when the
target is not usable yet, rather than switching into a mode that then fails
the first task. `MakCompleter` (§12) offers `/local`'s sub-commands and
`/mode`'s three values in the argument-completion position, mirrored in a
small local table rather than importing `cli/local.py` — so every keystroke
does not pay for `prompt_toolkit`'s styles and `rich`'s progress-bar imports.

### `/endpoint` — custom OpenAI-compatible endpoints (Wave 22)

`cli/endpoints/` is the interactive counterpart of the `endpoints:` config
section (§11): `list`, `add`, `show`, `edit`, `test`, `models`, `remove`,
`export`, `help`. `add` runs a gather-then-commit wizard (`wizard.py`) — every
question is asked before anything is written, and a `CANCELLED` sentinel at any
step discards the whole draft rather than leaving a half-filled entry; picking a
preset (nvidia, openrouter, deepseek, zai-general, zai-coding) prefills the URL
and credential variable from `mak/endpoints/profiles.py`, or `custom` starts
from nothing for a service MAK ships no profile for. `export <id>` prints the
same secret-free YAML block documented in §11, ready to paste into `mak.yaml`.
`test <id>` runs the endpoint's configured health policy (`models` / `chat` /
`none` — see "Universal OpenAI-compatible endpoints" in the history section
below) on demand — the one place `/endpoint` is allowed to spend a request, and
only on an explicit ask.

Endpoints added this way are **saved**, not session-only — they persist across
runs in `~/.config/mak/endpoints.json` (`mak/endpoints/store.py`, atomic `0600`
write, schema-versioned) so they survive the process the wizard ran in. This is
the one deliberate exception to "Session-only configuration" below, alongside
`/local`'s save prompt: an endpoint is infrastructure a user configures once and
reuses across many `mak` invocations, not a per-run override like `/models` or
`/max-agents`. A project's own `mak.yaml` `endpoints:` entries and the user
store are merged (`merge_endpoints`), with the project file winning on a
matching id — so a team can commit shared endpoints while an individual still
keeps personal ones.

Credentials are never part of this file or this flow: `add`/`edit` ask for an
**environment variable name**, never a key value, and `/apikey` (below) is where
the value itself is written to `~/.config/mak/.env`.

**Adding a new slash command:** add a handler in `commands.py` (print a one-line
`print_ok`/`print_warn`/`print_error` confirmation if it mutates state — the
bottom toolbar shows live state automatically), register it in
`handle_command()`, add its entry to `COMMANDS` in `completer.py` (that single
list drives both the `/` menu and `/help`), and add argument completions in
`MakCompleter` if it takes arguments. Return `"exit"` or `"clear"` from
`handle_command()` for commands the main loop must act on.

### Session-only configuration (design constraint)

All changes made via slash commands (`/models`, `/work-dir`, `/max-agents`,
`/planner`, `/no-review`, `/config`, `/local`, `/mode`) are **session-only**.
They live in `CliState` in memory and are **never written back to
`mak/config.yaml`** or any other file — `/local`'s own save prompt is the one
deliberate, explicit exception, and even that writes only on a "y" answer
(§12.2 above).

`cli/runner.py` enforces two invariants that protect this:

1. **No config file reference passed to MAK.** `build_session()` builds an
   in-memory `MakConfig` from `CliState` overrides and passes only that object to
   `mak.__main__.build_session`. The config file path (`state.config_path`) is
   **not** included in the `args` object forwarded to MAK — removing any pathway
   for a future MAK refactor to write back to `mak/config.yaml`. That object is a
   real `argparse.Namespace` (Wave 17), not a `SimpleNamespace` lookalike: the
   latter satisfied `build_session` by duck-typing today, but `mypy --strict`
   flagged it the moment the gate was extended to `cli/` (see "The quality gates"
   above) — a `SimpleNamespace` gives no static guarantee that it carries every
   attribute `build_session` reads, and would have broken silently the first time
   it read a new one.

2. **`mak_dir` is anchored to `work_dir`.** `_apply_state_to_config` calls the
   shared `config.anchor_mak_dir` (§11, Wave 17) rather than its own inline
   version — a relative `mak_dir` (the default `".mak"`) resolves to an absolute
   path inside `work_dir`, an absolute override is left alone. Without this, the
   node store, lock table, and session log would be created inside the MAK kernel
   repo (relative to process CWD) instead of the target project. This invariant
   used to hold only here: `mak run` had its own, separate bug where `mak_dir`
   was interpreted against the process CWD regardless of `--work-dir`, so the two
   front ends disagreed about where a project's state lives. They now share one
   implementation and cannot drift apart again.

`run_session_in_thread` runs `session.run()` on a daemon thread and joins it. It
used to spin on `while t.is_alive(): time.sleep(0.05)` *and then* `join()` — the
loop changed nothing about when the function returned, since the join did all the
waiting, and burned a core for the length of every run (Wave 18 deleted it). If
progress ticks are ever wanted here, they belong on the session's log events, not
on a polling loop.

If you add new CLI state fields that affect how MAK runs, apply them in
`_apply_state_to_config` (in-memory only) and never persist them to the config file.

### API keys

Keys are loaded by `cli/core/api_keys.py` — from `~/.config/mak/.env` (respects
`$XDG_CONFIG_HOME`; created `0600` by the setup wizard), with a source-checkout's
legacy `mak/.env` read at lower precedence and exported environment variables
winning over both — and stored in `CliState.api_keys`. The first-run wizard
(`cli/setup.py`) prompts and writes them to the user config dir. Keys are injected
into `os.environ` before each MAK session so the adapters find them via their
`api_key_env` fields.

**Any variable name, not a fixed set (Wave 22).** Before Wave 22, `save_keys`
only knew the three built-in providers' env var names and rewrote `.env` from
that fixed set on every save — harmless while only three names existed, but it
would have **deleted** an endpoint's credential the next time any key was saved,
since a name it didn't recognize simply wasn't in what it wrote back. `save_keys`
now parses the existing file, merges in only the names it was asked to set or
clear, and renders the rest byte-for-byte unchanged (`cli/core/api_keys.py`,
`parse_env_file`/`EnvLine`) — an atomic temp-file-plus-`os.replace` write, `0600`,
same as before. `key_names_for(endpoints)` collects the `api_key_env` names an
endpoint set actually needs, so `/apikey` can prompt for exactly those alongside
the three built-in providers.

### Dependencies added by `cli/`

- `prompt_toolkit` — REPL input, history, slash menu, auto-suggest, bottom toolbar
- `rich` — all terminal rendering (welcome box, plan list, progress bars, spinners)

## 13. Model catalog

`mak/models/` (Wave 14) answers *"which models does each provider currently
offer"*. It **never** answers *"which model does MAK use"* — that stays exactly
where §11 puts it: `planner.model` / `agents[].model` in `config.yaml`, the CLI's
sole write path. A catalog refresh **never writes to `config.yaml`**; this is
enforced by an acceptance test that byte-compares the file across a refresh.

**Facts vs. judgment — the load-bearing split.** A `ModelEntry` carries *facts*
(`display_name`, `context_window`, `max_output`) fetched from the provider, and
*judgment* (`recommended`, `planner_ok`, `planner_recommended`) that MAK **never
infers**. Judgment comes from exactly one place: the hand-maintained, exact-model-id
table `mak/models/curation.py::CURATED`. There are no heuristics, no capability
thresholds, no id-pattern scoring — a model absent from `CURATED` gets
`Judgment()` (neutral: usable, unstarred, unwarned), and stays that way until a
human edits the table. A regression test (`test_only_curated_ids_carry_stars`)
pins this: nothing may make a model self-recommend by looking premium.

Separately, `curation.py::DENY` filters out endpoints that are not text-chat
models at all — `dall-e-*`, embeddings, TTS, image/music generation — plus a
dated-snapshot rule that collapses `claude-haiku-4-5-20251001` onto its undated
alias `claude-haiku-4-5` (a fact about provider naming, canonicalized against the
packaged seed's known aliases, not an opinion about quality). This eligibility
filtering is automatic; judgment is not, and the two must not be conflated.

**Persistence and schedule.** `manifest.py` caches each provider's fetched models
in `~/.config/mak/models.json` (atomic write: temp file + `os.replace`), never in
the package (a wheel install's package directory is read-only). `is_refresh_due`
implements **catch-up** scheduling on the 1st and 15th of each month: skip the
15th, start MAK on the 19th, and it refreshes on the 19th — it does not wait for
the next 1st. A 6-hour cooldown after a failed attempt (`is_in_cooldown`) stops a
permanently-offline machine from paying a fetch timeout on every single start.

**Failure isolation.** `refresh.py` replaces a provider's cached entries **only**
on that provider's own successful fetch. No key, a timeout, a malformed response,
or an unhandled SDK exception all mean "keep what we had" for that provider alone
— one provider failing never empties the catalog or blocks the others
(`mak/models/refresh.py::refresh`). A model a provider stops offering is not
deleted; it is marked `retired=True` so it stays visible (and still selectable)
rather than silently disappearing out from under a user who has it configured.
`ModelRegistry.recommended_planner` skips retired entries when auto-selecting, so
a retired favorite degrades gracefully instead of being handed back as the pick.

**Runtime registry.** `registry.py::ModelRegistry` composes, on every load: the
packaged `seed.json` (the offline floor — a user with no keys and no network
still gets a usable list) → the manifest's cached facts → `judgment_for()`
re-applied fresh every time, so editing `CURATED` takes effect immediately without
a refetch. The catalog snapshot is one immutable tuple; a background refresh
thread builds a new tuple and assigns it in one statement, so reads never need a
lock and never see a partially-built list.

**Triggering a refresh.** `maybe_auto_refresh` runs on a daemon thread from
`cli/app.py::_init_state`, gated by `models.auto_refresh` (§11) and the
`MAK_NO_MODEL_REFRESH` environment variable; it never prints, because writing to
the console from a background thread corrupts a live `prompt_toolkit` session —
results simply show up in `/models`, `/planner`, and the `/status` catalog line
next time they're queried. `/refresh-models` (`cli/commands.py`) is the
synchronous, schedule-ignoring counterpart: it prints a per-provider added/removed
diff and is how a model released between scheduled ticks becomes usable
immediately, without waiting for the next 1st or 15th.

**`cli/core/models.py`** is now a thin adapter, not the source of truth: it
re-exports `mak.models.ModelEntry` as `ModelInfo` (one dataclass, not two — its
`api_key_env`/`adapter_type` are derived `@property`s so existing call sites in
`cli/commands.py` and `cli/completer.py` are unchanged) and calls a module-level
`ModelRegistry()` singleton. There is deliberately **no** module-level
`ALL_MODELS` list anymore — a list captured at import time cannot reflect a
refresh; call `all_models()` instead.

**Endpoint-scoped catalogs (Wave 22).** A third-party OpenAI-compatible service
has no curated judgment table and no seed data — `sources_for_endpoints` builds
an `OpenAiCompatibleSource` per configured endpoint and `refresh(key_envs=...)`
fetches each in isolation, same failure-isolation guarantee as the three
built-in providers: one endpoint's fetch failing never empties another's list.
Every `ModelEntry` now carries `endpoint_id`, and the on-disk manifest moved to
**schema v2**, keyed by `(endpoint_id, model_id)` instead of `model_id` alone —
two services can offer a model of the same id (`meta/llama-3.3-70b-instruct` on
both NVIDIA and a private gateway) and they are two independent entries, never
one overwriting the other. A v1 manifest from before this wave is read and
migrated forward automatically; nothing under a user's existing three-provider
setup changes shape. `ModelRegistry.for_endpoint(endpoint_id)` and
`find(model, endpoint_id)` are the new lookup surface; `evaluated` (whether a
model has passed curation at all) is **derived from the provider at load time**
rather than persisted, so a manifest cannot go stale about which entries are
first-party.

## 14. Local runtimes (`mak/local/`)

`mak/local/` is `mak/models/`'s counterpart for models that are not hosted
(Wave 15, D9). It is a **separate package on purpose** — the two answer
structurally different questions, and folding one into the other would have
bent both shapes to fit neither well.

**Why not just extend `mak/models/`.** Every provider in the catalog is keyed
by an API-key environment variable and refreshed from that provider's hosted
list-models endpoint (§13). A keyless, per-user-URL "provider" fits none of
that: it would touch `PROVIDER_ORDER`, `PROVIDER_KEY_ENV`,
`KEY_ENV_TO_PROVIDER`, the manifest schema, curation, retirement marking, and
the TUI's three `_KEY_ENV` maps — real machinery, built for a fact that a
local runtime doesn't have. So `mak/local/` asks **the running server what it
has, live, every time**: no cache, no manifest, no retirement marking,
because a local model list is authoritative, instant, and changes the moment
the user runs `ollama pull`. It borrows exactly one thing from `mak/models/`:
the **fact / judgment split** — `recommended.py` is the judgment half, and
nothing infers into it.

- **`ollama_client.py`** — a dependency-free HTTP client over Ollama's native
  API: `urllib.request` + `json`, one `Request` per call, no shared socket
  state. Three reasons, in order of weight: a fully-local install then needs
  **no third-party provider SDK at all** (the strongest possible form of this
  wave's promise, and why the `[local]` packaging extra, §13/pyproject, is
  empty rather than a dependency list); the surface MAK needs is five
  endpoints of plain JSON; and the `ollama` SDK pins `httpx` versions that
  would have to be reconciled against `openai`/`anthropic` for no gain.
  `OllamaClient` exposes `version()`, `list_models()`, `show(model)` (context
  length, read by **key suffix** across `model_info` —
  `"<architecture>.context_length"` — because the architecture is not knowable
  up front), `running()`, `chat(...)` (`stream: false` — the endpoint is on
  localhost, so a single blocking POST under the per-agent timeout is
  simpler than defending an idle streamed connection nothing will drop), and
  `pull(model)` (streams NDJSON progress, interruptible — a `KeyboardInterrupt`
  closes the response and leaves Ollama's partial blob alone, so a re-run
  resumes rather than restarts). Every transport/HTTP/timeout/JSON failure
  becomes a typed `OllamaError` naming the endpoint and the reason — nothing
  else escapes, because "the server isn't running" is the single most common
  failure here and must read as one clear line, not a `URLError` traceback
  out of a worker thread. The client holds no state across calls, so it is
  trivially safe to share across the session's worker pool, and it is
  injectable everywhere it is used — no test in `tests/local/` opens a socket.
- **`runtime.py`** — `LocalRuntime` (`kind`, `name`, `base_url`, `version`,
  `models`), a **value object**, not a live handle, so the TUI can hold,
  list, and compare probe results without holding connections open. Two
  kinds: `"ollama"` (probed via `/api/version` then `/api/tags`) — the one
  MAK reaches natively, because only it can size its own context window
  (§7.7/D11) — and `"openai_compatible"` (probed via `/v1/models`), which
  covers everything else: LM Studio, vLLM, llama.cpp's server, LocalAI.
- **`discovery.py`** — `discover(*, extra_urls=(), timeout=0.4, prober=None)`
  scans the well-known local ports **concurrently** (one short-lived thread
  per address under a `ThreadPoolExecutor`, sub-second timeout each) and
  returns whatever actually answered, Ollama first, deduplicated by
  `base_url`. It **never raises** — discovery runs on a UI path (first-run
  setup, `/local`) and during startup, where "nothing is running" is an
  ordinary, expected result, not a failure that should be able to take a
  session down with it; even a prober that violates that contract is caught
  and treated as "not there" rather than propagated. `$MAK_LOCAL_BASE_URL`
  (the same env var `agents_from_specs`'s `local:` provider falls back to,
  §12.1) and any `extra_urls` are scanned last and deduplicated against the
  well-known ports.
- **`recommended.py`** — the judgment half, mirroring
  `mak/models/curation.py`'s discipline exactly: a hand-maintained table of
  **exact Ollama tags** (a suggestion that cannot be pasted into `ollama
  pull` is not a suggestion), each with a rough download size and a
  one-line "what it's for", explicitly ordered **smallest first** so a
  wizard's default suggestion is the one most machines can actually run.
  `RecommendedModel.is_small()` is what `/local`'s planner step (§12.2) and
  `warn_local_planner_mismatch` (§8) read to recommend a cloud/hybrid
  planner — **MAK does not benchmark or rank local models; a human edits
  this list**, the same sentence `mak/models/curation.py`'s `CURATED` table
  carries.

Non-goals, stated once here because they shape the whole package: **MAK never
manages the Ollama daemon** — it detects, reports, and instructs, but never
runs, stops, or installs `ollama serve` itself, the same way it never
manages a CLI agent's binary (§7.6). And there is **no native adapter** for
LM Studio, vLLM, llama.cpp, or LocalAI — they are covered by `local_api`
(§7.7), and a native API per runtime would be a maintenance surface for a
marginal gain; Ollama is the one exception, and D11 is why.

---

# Part III — Developing

## Prerequisites

- **Python ≥ 3.11** (the project uses `match`/`StrEnum`/modern typing).
- **git** on `PATH` (the git integration shells out to it).
- The agent SDKs (`anthropic`, `openai`, `google-genai`) install automatically as
  dependencies; they're imported lazily, so the test suite never needs a live key.

## Setup

```bash
git clone <repo-url>
cd multi-agent-kernel

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -e ".[dev]"             # adds mypy, pytest, types-PyYAML
                                    # (ruff comes with the base install — §3.4)

pre-commit install                  # optional: run the gates on every commit
```

Copy the env template if you'll make real calls (the CLI auto-loads `mak/.env`):

```bash
cp mak/.env.example mak/.env        # then fill in the keys you use
```

## Project layout

```
cli/                       # interactive CLI app (prompt_toolkit + rich)
├── __main__.py            # entry point: MakCli().run()
├── app.py                 # MakCli: main REPL loop, task execution, token accounting
├── commands.py            # slash-command handlers (/models, /planner, /work-dir, /no-review, …)
├── completer.py           # MakCompleter: tab-completion for all slash commands
├── runner.py              # MAK library bridge + token counter + git diff helpers
├── setup.py               # first-run API key setup wizard
├── ui.py                  # all Rich rendering (welcome box, status, plan list, diff)
├── core/
│   ├── api_keys.py        # parse/merge/render ~/.config/mak/.env (Wave 22: any
│   │                      #   var name, not a fixed set — §12.2)
│   ├── models.py          # thin adapter over mak/models/ (ModelInfo = ModelEntry)
│   └── state.py           # CliState dataclass (models, agents, workdir, approval flag)
└── endpoints/              # /endpoint: add/edit/list/show/test/models/remove/export
    ├── commands.py         #   (Wave 22, §12.2)
    ├── wizard.py           # gather-then-commit add/edit flow, CANCELLED sentinel
    ├── prompts.py
    └── render.py           # table rendering + secret-free YAML export

mak/
├── __main__.py            # CLI entry point: python -m mak --task "..."
├── bootstrap.py           # composition root: build_registry / default_agent_type / validate_config
├── config.py              # config loading + validation
├── config.yaml            # default configuration
├── session.py             # session lifecycle, transactional commit, recovery
├── cascade.py             # the shared post-wave fix-up loop + CascadeOutcome
├── execution_result.py    # ExecutionResult: the whole run's outcome, not one wave
├── teardown.py            # SuiteOutcome / TeardownResult + the push policy
│
├── core/
│   ├── types.py           # NodeId, NodeFragment, LockEntry, TaskBundle, …
│   ├── exceptions.py      # all MakError subclasses
│   ├── logging.py         # append-only JSON-Lines session logger
│   └── budget.py          # resolve_output_budget: shared catalog-driven token
│                          #   budget resolver (Wave 12, §7.2.1)
│
├── endpoints/              # universal OpenAI-compatible endpoints (Wave 22, §7.3/§11)
│   ├── types.py            # Transport/Location/HealthPolicy/StructuredOutput enums,
│   │                       #   EndpointConfig — no I/O, no mak.config import
│   ├── profiles.py         # the six built-in presets — single source of truth
│   │                       #   for every preset URL and key-env name
│   ├── parse.py            # YAML → EndpointConfig; identity materialized,
│   │                       #   capabilities left deferred
│   ├── resolution.py       # the precedence walk: explicit > profile > transport
│   ├── builtin.py          # synthesizes built-in endpoints for the legacy
│   │                       #   hosted/local agent types
│   ├── agents.py           # resolve_agents / derive_agent_id / unique_agent_id
│   ├── store.py            # per-user ~/.config/mak/endpoints.json, atomic 0600
│   ├── capabilities.py     # the structured-output ladder, the rung→parameter
│   │                       #   table, catalog-seeded start rungs, and the
│   │                       #   single-flight session CapabilityCache (Wave 24)
│   ├── error_classification.py  # why a structured request was refused —
│   │                       #   parses the provider's error body, not str(exc)
│   │                       #   (Wave 24)
│   └── health.py           # failure classification + the three health policies
│
├── node_store/
│   ├── ingestion.py       # file → raw-source span-tiled fragments
│   ├── makignore.py       # .makignore: gitignore-style per-project ignore list
│   ├── store.py           # NodeStore: versioned get/put/commit/rollback/revert,
│   │                      #   transaction(), sync_file(), retire_node()
│   ├── journal.py         # write-ahead commit journal + restart recovery
│   ├── transaction.py     # render-all → journal → atomic install, as one step
│   └── reconstruction.py  # fragments → file (assemble + ruff format)
│
├── lock_manager/
│   ├── rwlock.py          # per-node reader-writer lock
│   ├── lock_table.py      # thread-safe lock state + persistence + leases
│   ├── project_lease.py   # OS-backed single ownership of one project (flock)
│   ├── conflicts.py       # the single canonical conflict matrix
│   └── deadlock_detector.py
│
├── scheduler/
│   ├── dag.py             # DAG build + validation + topological order
│   └── scheduler.py       # tick loop, atomic lock pre-allocation, persistence
│
├── conflict_detector/
│   ├── detector.py        # orchestrates the checks
│   ├── signature_check.py
│   ├── import_check.py
│   └── name_collision_check.py
│
├── planner/
│   ├── planner.py         # LLM decomposition, SubTask schema, retry logic,
│   │                      #   config-gated outline strategy + self-critique
│   ├── llm.py             # PlannerLLM completion backends (build_planner_llm)
│   ├── response.py        # tolerant JSON extraction; truncated vs malformed
│   ├── review.py          # human-in-the-loop DAG review + finding rendering
│   ├── depgraph.py        # static call/import dependency graph (Wave 10)
│   └── validation.py      # deterministic plan grounding/augmentation (Wave 10)
│
├── agent_runner/
│   ├── runner.py          # routes to API/subprocess adapters; failure policy
│   ├── registry.py        # AdapterRegistry (instance, not global)
│   ├── protocol.py        # TaskBundle/TaskResult wire (de)serialization
│   ├── sandbox.py         # Docker isolation for CLI agents (--sandbox)
│   ├── stop_signals.py    # provider stop-signal check + usage normalization
│   │                      #   shared by all three API adapters (Wave 12, §7.2.1)
│   ├── adapters/
│   │   ├── base_adapter.py
│   │   ├── budget.py                  # agent-shaped output budget + stop-signal
│   │   │                              #   vocabulary (Wave 12, §7.2.1)
│   │   ├── anthropic_api_adapter.py   # primary
│   │   ├── openai_api_adapter.py      # primary
│   │   ├── gemini_api_adapter.py      # primary
│   │   ├── cli_adapter.py             # shared CliSubprocessAdapter base
│   │   ├── claude_code_adapter.py     # secondary (claude CLI)
│   │   ├── codex_adapter.py           # secondary (codex CLI)
│   │   └── copilot_adapter.py         # secondary (gh copilot CLI)
│   └── wrappers/          # bridge: MAK protocol ↔ a real CLI's I/O
│       ├── bridge.py      # decode bundle → prompt → invoke CLI → TaskResult
│       ├── claude_code.py # python -m …wrappers.claude_code (drives `claude`)
│       ├── codex.py
│       └── copilot.py
│
├── models/                # self-refreshing model catalog (Wave 14, §13)
│   ├── catalog.py         # ModelEntry, provider maps, packaged seed.json loader
│   ├── curation.py        # hand-maintained judgment table + eligibility filters
│   ├── manifest.py        # atomic per-user cache, schema version, 1st/15th schedule
│   ├── providers.py       # anthropic/openai/gemini list-models fetchers
│   ├── refresh.py         # per-provider add/remove diffing, failure isolation
│   ├── registry.py        # ModelRegistry: seed+manifest+curation snapshot, auto-refresh
│   └── seed.json          # packaged offline-floor catalog
│
├── test_runner.py         # build the teardown TestRunner from session.test_command
└── git_integration/
    └── git.py             # audit-log commits, log parsing, push

tests/                     # mirrors mak/ package-for-package
.github/workflows/ci.yml   # CI: ruff + mypy --strict + pytest
.pre-commit-config.yaml    # local hooks mirroring CI
pyproject.toml             # deps, ruff + mypy + pytest config
```

Tests mirror the source tree (`tests/core/`, `tests/node_store/`,
`tests/lock_manager/`, `tests/scheduler/`, `tests/conflict_detector/`,
`tests/agent_runner/`, `tests/planner/`, `tests/models/`, `tests/git_integration/`,
`tests/test_config.py`, `tests/test_bootstrap.py`, `tests/test_session.py`). No test
in `tests/models/` touches the network — every provider fetch is a fake `ModelSource`.

## The quality gates

Three gates must be green for every change — locally, in pre-commit, and in CI:

```bash
pytest -q                  # the full suite (currently 2528 tests)
mypy --strict mak cli      # zero errors
ruff check mak cli tests   # zero findings
```

`mak cli` and `mak cli tests` — not just `mak` — since Wave 17 extended both gates to
`cli/`. That extension found real defects on the first run: a `Session._planner`
access with no `None` guard (latent — `build_session` always sets one today, but a
bare `AttributeError` in a worker thread the day it doesn't) and a `SimpleNamespace`
passed where `mak.__main__.build_session` expects a real `argparse.Namespace` (worked
by duck-typing; would have broken silently the day `build_session` read a new
attribute). Extending a gate is not free of cost, but it is exactly the kind of
defect a gate exists to catch before a contributor does.

CI (`.github/workflows/ci.yml`) runs all three on push and PR against `main`; the
pre-commit hooks mirror them. A change that breaks any gate will not merge.

To run a focused subset while iterating:

```bash
pytest tests/node_store/ -q
pytest tests/test_session.py -q
```

If you add a feature, add tests for it. If you change ingestion or reconstruction,
the round-trip property test (`tests/node_store/test_roundtrip.py`) is mandatory; if
you change locking, keep the concurrency stress test
(`tests/lock_manager/test_concurrency.py`) green.

## Coding standards

Python throughout, with these conventions (enforced by `ruff` and `mypy --strict`):

**Naming**
- `snake_case` for variables, functions, modules; `PascalCase` for classes;
  `UPPER_SNAKE_CASE` for constants; `_leading_underscore` for private members.

**Structure**
- One module, one responsibility — don't co-locate unrelated logic.
- Functions do one thing. If one exceeds ~40 lines, ask whether to split it.
- **No global mutable state.** Pass state explicitly via arguments or dataclass
  instances. (The registry being an instance rather than a module-global dict is a
  direct consequence of this rule.)
- Use `dataclasses` for structured data — no raw dicts as function arguments.
- **Type annotations are mandatory** on every function signature; the codebase is
  `mypy --strict` clean and must stay that way.

**Imports**
- Standard library, then third-party, then internal (`mak.*`), separated by blank
  lines (ruff's isort enforces this; `mak` is configured as first-party).
- Never use wildcard imports.

**Error handling**
- Explicit exceptions with descriptive messages; define domain exceptions in
  `mak/core/exceptions.py`.
- Never silently swallow an exception — log and re-raise, or handle deliberately.

**Comments & docstrings**
- Public functions and classes require docstrings (ruff enforces this in `mak/`;
  tests are exempt).
- Inline comments explain *why*, not *what*.
- **No TODO comments in committed code** — open a tracked issue instead.
- **Keep comments self-contained.** Do not reference internal planning artifacts or
  documents that aren't part of the committed tree — a comment must make sense to a
  contributor who only has the source in front of them.

## Commits, branches, and pull requests

- **Branch off `main`.** Don't commit directly to `main`.
- **Keep PRs scoped.** One logical change per PR; keep the three gates green in every
  commit you push where practical, and certainly in the final state.
- **Write descriptive commit messages** — explain the *why*. (Note: the `[MAK-<id>]`
  commit subject format is what the *kernel* writes for agent audit commits; your own
  development commits should follow ordinary good practice.)
- **Tests accompany behavior changes.** A PR that changes behavior without tests, or
  that drops the suite/`mypy`/`ruff` from green, will be asked for revision.
- **Update this file** when you change something it documents (a new subsystem, a
  config key, a workflow step). Because the internal planning docs are not part of
  the committed tree, `CONTRIBUTING.md` is the canonical reference contributors rely
  on — keep it accurate.

---

# Part IV — Where to contribute

Primary development is done — the kernel is built, gated, and proven. This part is
about **what's left**, ordered by leverage. Start here.

## Open problems

The v2 roadmap, in the project's current **priority order**. These are research and
tooling on top of a finished kernel — open an issue to align before starting a large
one.

### 1. Planner context / token efficiency

`Planner.decompose` lists the **entire** node inventory in the prompt on **every**
call — and again on every retry. For a large repo that is thousands of lines of input
re-sent each time, even though the inventory barely changes; planner tokens aren't
even measured today. Directions (not mutually exclusive): **prompt caching** of the
stable inventory prefix (Anthropic `cache_control`, OpenAI/Gemini equivalents — needs
extending the `PlannerLLM` interface from `complete(prompt)` to a cacheable
prefix/suffix); **retrieval** of only task-relevant nodes (keyword/embedding); a
**coarse→fine** two-stage planner (pick modules, then decompose within them); a
module-level **summarized inventory**; and a **template bypass** for fixed task shapes.
Start by measuring planner tokens, then add caching (helps retries immediately), then
selection. Acceptance: planner input scales sub-linearly with repo size.

### 2. Multi-language support

Ingestion, reconstruction, and the conflict detector's checks are Python-`ast`-specific;
everything else (node-store schema, locks, scheduler, session, transport) is already
language-agnostic. Plan: a **`LanguageBackend` ABC** (`parse_into_fragments`,
`reconstruct`, optional structural checks, extension routing) with **tree-sitter** as
the parser — its precise node ranges let the same raw-source span-tiling model
generalize, so comments/formatting survive by construction. Phases: (A) extract the
Python backend behind the ABC (pure refactor, no regression); (B) a **TypeScript**
backend end-to-end, gated by the round-trip property test; (C) generalize the conflict
detector (parse-gate-only baseline for new languages first); (D) Go/Rust; (E)
mixed-language repos. Per-language pieces: a node-identity scheme and a formatter
(`prettier`/`gofmt`/`rustfmt`, discovered with fallback like `ruff` today).

### 3. Deployment — PyPI + Docker

MAK is only runnable from a clone today. **Decision: PyPI (with `pipx` as the
recommended install) is primary; an official Docker image is secondary; Homebrew is
deferred** (it duplicates pip for a Python CLI). **Prerequisite (do first): config
discovery + scaffolding** — `--config` defaults to `mak/config.yaml`, which only exists
in the repo, so an installed `mak` needs config discovery (`./mak.yaml`,
`~/.config/mak/…`, built-in defaults) and a **`mak init`** command; MAK also reads keys
from the environment and doesn't auto-load `.env`. Then: a `mak` console entry point in
`pyproject.toml`, **optional extras** (`[anthropic]`/`[openai]`/`[local]` — the three
SDKs are heavy), a CI release workflow (OIDC trusted publishing), and a `Dockerfile`
that bind-mounts the target repo (`docker run -v "$PWD:/work" …`).

### 4. Local LLM support — **[RESOLVED — Wave 15]**

**Shipped in Wave 15.** Two new agent types — `local_api` (any OpenAI-compatible
server: vLLM, LM Studio, llama.cpp, Ollama's compat layer) and `ollama_api` (a
dependency-free native client that can size its own context window, which the
OpenAI-compatible transport structurally cannot) — plus a matching
`OllamaPlannerLLM` and explicit backend resolution for the planner. Structured
output is configurable per transport with a one-shot automatic downgrade, and
both local adapters share one parse→repair→retry loop instead of paying a
full bundle re-dispatch for a malformed reply. The interactive app gained a
first-class `mode` (`cloud`/`local`/`hybrid`) and a `/local` wizard that
detects a runtime, pulls a model, and completes a task with **no API key
anywhere** — see §7.7, §8, §12.2, and the new §14 for the detail, and
`TASKS.md`'s Wave 15 section (design decisions D1–D13) for exactly what was
built and why. Four deviations from the original sketch below, all decided
for concrete reasons rather than scope creep: **(a)** two agent types sharing
one adapter class, not one type with an added field — the registry is keyed
by type, so one type could never run cloud and local together; **(b)**
malformed replies are repaired in the adapter with one short follow-up turn,
not by re-dispatching the whole bundle; **(c)** Ollama gets a genuine native
adapter (stdlib HTTP, no SDK) instead of routing through the OpenAI-compatible
transport, because only the native API can read and set the model's real
context window — the compat layer's `num_ctx` is not an OpenAI parameter, and
Ollama silently truncates an over-long prompt rather than erroring on one;
**(d)** the app's mode is explicit state rather than something inferred from
what keys happen to be set, because "cloud or local" changes which surfaces
validate against what, and inferring it wrongly is exactly the kind of
silent surprise this wave exists to remove.

### Also: extend the benchmark

The recorded [`benchmark/`](benchmark/) run is single-model (billing limits) and
maximally-contended. Make it representative: more model mixes, larger and
*partially*-contended workloads (to show MAK's parallelism on independent work), harder
tasks (to open the accuracy gap), and a throughput variant — turning one data point
into a curve. See [Benchmark: MAK vs. git
worktrees](#benchmark-mak-vs-git-worktrees).

## Known limitations (accepted tradeoffs)

These are deliberate limits of an intentionally shallow, correctness-first kernel.
None can corrupt code — each *fails safe* — so they are accepted tradeoffs, documented
here so contributors don't mistake them for bugs:

- **Class-shell fragments aren't independently parseable.** A `class Foo:` shell with
  its methods removed isn't valid Python alone, so a task that targets a `class`-shell
  node and returns just the shell is *rejected* by the parse gate (never corrupted).
  Reconstruction validates the assembled file, so this is safe; making shells
  standalone-parseable (or the detector shell-aware) is a possible improvement.
- **The conflict detector is name-based and shallow.** It is a structural gate, not a
  type checker, by design. Wave 11 closed the worst of this for *methods* — an
  attribute call only resolves through `self`/`cls`/the owning class name, never by
  bare method name, so a call on an untyped receiver is skipped rather than guessed
  at (§5.1). The remaining exposure is **module-level functions**, which are still
  keyed by bare name across the whole merged batch: a bare `foo(...)` can flag
  against a same-named-but-unrelated top-level function defined in another file in
  the same round — a false positive that costs a bounded retry, not correctness.
- **`context_nodes` are read-locked at dispatch.** The scheduler acquires a WRITE
  lock on each target node and a **READ lock** on each `context_node` (deduped against
  the write set) in the same atomic `try_acquire_all`, so a concurrent task cannot be
  rewriting a node this task reads as context. Multiple readers coexist; a writer
  waits for readers and vice-versa (the canonical conflict matrix). Atomic
  pre-allocation keeps this deadlock-free, and `from_persisted` restores
  `context_nodes` so recovery re-takes the read locks. A read lock on an id with no
  committed fragment is legal — the lock table is keyed by id and never consults the
  node store — which is what lets a context node another task is about to create
  survive validation (Wave 13, §8). A *hard* dependency on another task's output
  still belongs in `depends_on` (the DAG enforces ordering, and since Wave 13 also
  delivers that output as context); `context_nodes` are the *soft* reference layer,
  and a forward one now has its ordering edge added for it.
- **The deadlock watchdog never fires.** Atomic lock pre-allocation means a waiting
  task holds no locks, so the wait graph is acyclic by construction. The
  `DeadlockDetector` runs each iteration as genuine defense-in-depth that, by design,
  finds nothing.
- **A wedged worker's abandonment is cooperative, not preemptive (Wave 17).**
  `Session.close(wait=False)` shuts the thread pool down with
  `cancel_futures=True` on the abnormal-exit path, and that call cannot actually
  interrupt a call already in flight — it only drops work still *queued*. What
  bounds the in-flight call is the per-request SDK timeout threaded from
  `AgentConfig.timeout` (§7.5); the two are a pair, and either one alone leaves a
  gap (a timeout with no non-blocking shutdown still hangs the process joining the
  worker thread when it eventually raises; a non-blocking shutdown with no timeout
  never actually bounds how long the abandoned call runs, only how long the
  *session* waits for it). This is accepted rather than fixed further because
  Python offers no safe way to kill a thread mid-call; the alternative is running
  every agent call in a subprocess, which is a materially bigger change than this
  wave's scope.
- **One MAK per project, enforced (Wave 19).** Two concurrent runs on one checkout
  are refused with `ProjectBusyError`, not scheduled. This is a deliberate scope
  limit: MAK is a tool a person runs on their own working tree, and the alternative
  — a genuinely distributed lock table over the persisted state — is machinery for a
  workload MAK does not have. Distinct projects still run concurrently.
- **The Windows project lease is weaker than the POSIX one (Wave 19).** `flock` is
  released by the kernel when its holder dies, so abrupt-owner recovery on POSIX
  needs no heuristic. Windows' `msvcrt` byte-range lock can outlive its process, so
  that path falls back to a heartbeat-age threshold (`stale_after_s`, 90s) — which
  can, in principle, either break a live lease whose owner was stopped for longer
  than that or make a successor wait. CI is POSIX; the fallback is documented rather
  than tested against a real Windows kill.
- **Reconciliation adopts the working tree by default (Wave 19).** When a file
  changed since MAK last wrote it, `session.on_external_edit: "adopt"` takes the
  human's version as the newer truth and syncs the store to it. That is the right
  default — the alternative is overwriting someone's work — but it does mean MAK
  will happily build on an edit it never saw made. `"conflict"` is the opt-in for a
  project that would rather stop and look.
- **A retired node's metadata entry is kept forever.** Recording a deleted symbol
  without destroying its history means the id stays in `metadata.json` (flagged
  `retired`) even after `version_retention` has pruned its last version file. The
  entry is a few dozen bytes and keeping it is what stops `gc` treating the
  directory as an orphan; a store with an extremely high symbol churn would
  accumulate them. No sweep exists for this yet.
- **Behaviour changes behind an unchanged signature need a gate (Wave 20, D5).**
  A function that starts returning `None` instead of raising, or reorders a
  list, or switches units, is invisible to every static check in §5.2 — the
  interface fingerprint is unchanged by construction. `semantic.impact_tests`
  catches it (that is exactly what shape 3 in the corpus needs), but it is off
  by default because it runs the project's own test suite in overlay
  subprocesses. `mak/semantic/overlay.py` materializing subset states makes
  it cheap enough to try, but a proper differential-property-test gate (the
  wave's own design notes called this D5 and scoped it out) is still open.
- **Impacted-test selection is static, not coverage-driven (Wave 20).** The
  research definition calls for `coverage.py` dynamic contexts mapping test →
  node once at `initialize()`; `mak/semantic/impact_tests.py::select_tests`
  approximates it with the static import graph instead (a test file is
  selected when its import closure reaches a touched module). This is a
  superset of the coverage-based selection in the ordinary case and a
  reasonable approximation everywhere else, but a test that reaches a touched
  module only through late binding or dependency injection would be missed.
- **The new static checks (attribute/override/constructor/cycle/duplicate,
  §5.2) inherit signature_check's precision-over-recall contract, deliberately.**
  Each skips a call through an untyped receiver, a class with a metaclass or an
  unrecognised decorator, multiple resolved bases, and anything a module binds
  dynamically — the same class of gap §5.1 already documents and accepts for
  the exact same reason: a false positive costs a whole fix-up task, a missed
  one costs nothing the test suite (or, now, the optional gates) would not also
  catch.
- **Three pre-existing `TestIterSourceFiles` failures** in
  `tests/node_store/test_ingestion.py::test_matches_the_glob_it_replaces`
  predate Wave 20 (introduced by the `.makignore` work, one commit before it)
  and are out of this wave's scope; they are unrelated to semantic conflicts
  and left for whoever picks up ingestion glob matching next.

## Good first contributions

- Add test coverage for an edge case in an existing module (ingestion corner cases,
  conflict-detector splat handling, config coercion).
- Improve error messages — make failures point at the fix.
- Documentation: clarify a subsystem in this file, or add module-level examples.
- Harden a CLI bridge wrapper (`mak/agent_runner/wrappers/`): tighten a CLI's prompt
  or output parsing for a specific `claude`/`codex`/`gh copilot` version, or extend the
  sandbox (host allowlisting).

## How MAK was built (history)

MAK was built in gated **waves** — each a set of independent tasks that had to leave
the full suite, `mypy --strict`, and `ruff` green before the next began. That
discipline is why the foundation is solid. Waves 0–1 built the core (types, config,
logging, node store, lock manager, agent-runner base); Wave H hardened ingestion
(span-tiling for comments/decorators/methods), the concurrency model, and CI; Waves
2–3 added the scheduler, conflict detector, API adapters, planner + HitL, git
integration, and the session; Wave 3.5 + a hotfix hardened the session and wired the
composition root; Wave 4 delivered the CLI, secondary CLI adapters, and the sandbox;
**Wave 5** made dispatch concurrent and proved the shared-memory thesis with the
integration gate; **Wave 6** carried the agent's rewritten source over the wire so a
real agent's edit reaches the store.

Post-Wave 6 improvements: **context enrichment** extended to four layers (automatic
same-file sibling injection and cross-file caller scanning via word-boundary regex,
in addition to the planner's `context_nodes`); **dynamic cascade detection** —
`detect_cascade_tasks()` compares AST signatures before/after each wave and
generates a CASCADE WAVE for the user to review if callers need updating; **planner
CASCADE PREVENTION** instructions that push the planner to include caller tasks
upfront; **`compile()` everywhere for validation** — replaced `ast.parse()` in every
validation gate so that `from __future__` placement and other compile-time rules are
enforced before acceptance; **`get_preview_fragments`** added to `NodeStore` for
correctly-indented pre-commit preview assembly; **whole-file node primacy** —
`list_nodes` and `parse_file_into_nodes` enforce that a committed whole-file node
takes exclusive authority over its file, so stale fragments are excluded from
reconstruction (the *other* half of that rule — skipping re-ingestion entirely — was
how a human's edit to a whole-file node got silently discarded, and Wave 19 replaced
it with synchronization; see §2/§10); and **no-op disk sync** so that the on-disk file is always written from
the committed node store content when a no-op task completes. **Wave 10** (out of
numeric order — it didn't depend on Waves 7–9) added deterministic plan validation: a
static dependency graph over the node store (`depgraph.py`) grounds hallucinated node
ids and augments/flags a plan's `depends_on` edges against real code structure
(`validation.py`) before every `install_plan`, plus config-gated outline→detail
planning, a self-critique pass, and plan-quality metrics. **Wave 14** replaced the
hand-written model list (`cli/core/models.py::ALL_MODELS`) with a self-refreshing
catalog (`mak/models/`, §13): provider facts are fetched and cached with strict
per-provider failure isolation, while judgment (`recommended`/`planner_ok`/
`planner_recommended`) stays a hand-maintained exact-id table that MAK never
infers from — new models arrive usable but unstarred until a human curates them.
A follow-on **planner-robustness hotfix** fixed a failure that looked random and was
not: the planner asked for at most 4,096 output tokens, so a plan for a real repo was
cut off mid-string, and since the same request yields the same over-long plan, every
retry was cut at the same point and the run failed with
`response was not valid JSON: Unterminated string`. The budget now comes from the
model's own documented output limit (§8), each backend reports a provider-signalled
cut as `TruncatedResponseError`, `response.py` distinguishes truncated from malformed
rather than lumping both into "bad JSON", and a truncated attempt is retried with a
request for a *smaller* plan instead of an identical one. Raising the budget in turn
pushed the Anthropic planner call past the SDK's non-streaming ceiling, so that
backend now streams and assembles the final message.

**Wave 11** fixed three defects found by reading one real session's artifacts in
full — a run that ended 6 completed / 3 failed / 6 skipped, where every failure
traced back to MAK itself, not the planner or the agents (§5.1, §10, §7.4). The
signature check stopped rejecting correct Python (`@staticmethod` receivers,
attribute-call resolution, bare-name method shadowing), the node store stopped
ingesting its own `.mak/` persistence directory (an exact +325 nodes/run,
compounding), and a dropped agent result stopped being silent (`AGENT_RESULT` /
`SOURCE_DROPPED` events, specific failure reasons, and a node-granularity contract
enforced by `map_returned_sources` so a symbol id returned under a whole-file grant
is folded in rather than discarded). A false-positive/true-positive corpus
(`tests/conflict_detector/test_false_positive_corpus.py`) is the standing guard
against the first regressing.

**Wave 12** is the same defect class as the Wave 10 planner hotfix, one hop
downstream: MAK could not tell a truncated agent response from a deliberate
"nothing to change" (§7.2.1, §10). Wave 11's own `AGENT_RESULT` event is what
made this diagnosable in one pass on the next real run: 8 of 11 `agent_result`
events came back `success: true` with no returned nodes and no error, and two of
the four "completed" tasks (`marks`, `modes`) had in fact received no work — the
run reported `tasks_completed: 4.0` against real progress of 2 tasks out of 20.
Root cause: the Anthropic adapter's `max_tokens` was hardcoded at 8192 (6% of the
model's documented limit, and just under the size of the whole-file rewrites this
project actually emits), and nothing in any adapter read the provider's stop
reason, so a cut-off reply decoded into a valid, empty, *successful* result. Fixed
by taking the budget from the model catalog (shared with the planner's resolver),
checking `stop_reason`/`finish_reason` before any payload is read, and switching
the Anthropic adapter to streaming (the same non-streaming ceiling the Wave 10
hotfix hit). The more dangerous half was independent of the cause: the session's
no-op acceptance treated *any* empty success on an existing, valid file as "audited,
nothing to change" — which a truncation satisfies exactly — so it now requires the
agent's positive assertion (`no_changes_required`) before accepting a no-op, logs
it as `ACCEPTED_NOOP` distinct from ordinary completions, and counts it in its own
`tasks_noop` metric rather than folding it into `tasks_completed`. A retry after a
truncation now carries a compaction instruction instead of re-issuing an identical
request, and a refusal fails a task immediately instead of spending its whole
attempt budget re-earning the same refusal. Separately, a model returning
`modified_fragments` as a single object instead of an array used to raise
`TypeError: string indices must be integers` (misreported as `"api call failed"`);
every malformed shape now raises a named `AgentProtocolError` instead, and the
runner reports a decode failure as such rather than blaming the transport. A
table-driven "degraded response" test corpus per adapter (truncated/refused/
malformed/undecodable) is the standing guard: it asserts that none of those shapes
can ever produce `success=True` with an empty result.

**Wave 13** moved one hop further upstream again: Waves 11 and 12 were about what an
agent *returned*, this one is about what it was *given* (§3.2, §5, §8, §10). The next
real run ended 7 completed / 1 failed, and the single failure was the only honest
result — an agent that refused to write code against APIs it had never been shown.
Three of the seven "completions" had the same defect and guessed instead; one shipped
a `pick_banner(width)` call against a real `pick_banner(width, height)`, a `TypeError`
on first use that passed every gate MAK had, plus an import of a function its target
module does not define, wrapped in `try/except: pass` so the feature was silently
dead. The unifying cause: **MAK builds a dependency graph and then dispatches every
task without the source of the things it depends on.** Three fixes, in the order the
context was lost. Plan validation stopped deleting context nodes a *sibling task in
the same plan* creates — in a greenfield wave those modules do not exist yet by
construction, and one plan lost 14 of them — and now adds the ordering edge that
makes such a node readable by dispatch time. Enrichment gained a fifth layer that
carries a task's direct dependencies' committed output, budget-bounded and degrading
to a public API digest rather than to nothing; and its cross-file layer, which
derived its search symbols from `::name` segments only, stopped being a silent no-op
for the whole-file targets Wave 11's folding made normal. And the kernel now records
what it dispatched (`TASK_DISPATCHED`, plus `context_bytes` metrics) and *refuses* to
dispatch a bundle with no context at all to a task that has dependencies — the same
lesson as `AGENT_RESULT` and `stop_reason`: the fact that would have made the defect
obvious was never written down. A post-wave `cross_module_check` catches the
consequence as well as the cause: two modules created in one wave that disagree about
each other's API now surface as fix-up tasks instead of reporting clean.

**Wave 16** is Wave 13 read back from the next real run, and it is the wave that says
the most about why these get read at all. The run itself was clean — 9 completed, 0
failed, zero `context_dropped` where the previous three runs of the same project had
dropped 14, 21 and 34 — but the log it now emits showed what that cost: **261k input
tokens**, of which one task spent 151 KB (67,847 tokens) on context. Attributing it
found that Wave 13's own whole-file symbol derivation counted module-level
assignments, so `__all__` — which most well-formed modules declare — behaved as a
symbol and dragged in every module that declared one; 88% of that bundle matched on
nothing else, and `package_entrypoint` spent 47,607 input tokens to answer "no
changes required" in 87. Wave 13 had bounded layer 5 as instructed and left layer 4,
the one actually spending, with no ceiling. Both are fixed here: a symbol is now a
name that can be a *node id*, and the caller layer is ranked, evidence-filtered
(minimum length, maximum match count) and budget-bounded, which takes that bundle's
cross-file content from 151 KB to the 16.4 KB of genuine callers. The same read found
two guards that never reached the code they were built for: the cross-module check
ran only from `mak run`, so the interactive app skipped it entirely — including on a
`python -m editor` `TypeError` that very wave had created — and its import resolution
inherited validation's unique-last-segment fallback, which resolved
`from PyInstaller.__main__ import run` onto the repo's own `editor/__main__.py` and
would have generated a task telling an agent to break correct code. The loop moved to
`mak/cascade.py` so both front ends drive one implementation, and the gate now
resolves strictly. `TASK_DISPATCHED` also carries per-layer attribution now, because
doing this analysis by hand against `task_graph.json` is exactly the cost the event
exists to remove. What remains is the open-problems list above.

**Wave 17** is a different kind of read from 11–16: not one real session's log, but
a full security-and-robustness audit of the kernel (§2, §7.5, §10, §11), and it
found two defects load-bearing enough to reorder the roadmap around them. First,
**nothing checked where a node id was allowed to write.** A node id's file
component becomes a real filesystem path twice — the node store's fragment
directory, the reconstructed file under the work dir — and neither join was safe:
`Path(work_dir) / "/etc/x.py"` is `/etc/x.py`, discarding the work dir outright,
and `..` walked out of either root. The planner's existing `.py`-extension check
passed both cleanly. Verified against the pre-fix tree: a plan naming
`../../ESCAPED.py` and `/tmp/mak_abs_probe.py` as targets was accepted by
`parse_plan` without complaint, and a direct `NodeStore.put_node` under that id
wrote the file outside the store root. The planner is an LLM reading a node
inventory derived from repo *contents*, so this was reachable from an untrusted
repo, not just a malicious plan. Second, **`mak_dir` was interpreted against the
process CWD, not `work_dir`**, in the `mak run` path (the TUI had already fixed
this for itself and never shared the fix): two projects driven from one shell
shared one node store, and because node ids are work-dir-relative, a file with the
same relative path in each project was the *same id* — the second project silently
inherited the first's content.

Both are closed by containment checked at **every** boundary that turns an id into
a path, independently, rather than once at the top: `parse_plan` (lexical — no
work dir is in scope yet), the node store's `_fragment_dir` (resolving — catches a
symlinked escape a string check cannot), and `Session._reconstruct_affected`/
`install_plan`. That last one was not in the original plan — `install_plan` is
called directly by the interactive app and by every cascade wave, neither of which
goes through `parse_plan`, so without its own gate two of the three ways a plan
reaches the scheduler were ungated. `mak_dir` is now anchored under `work_dir`
(`config.anchor_mak_dir`, shared by both front ends) with a stale pre-anchor `.mak`
reported on stderr and never adopted — guessing that an orphaned store belongs to
*this* project would reintroduce the exact corruption the fix removes. Alongside
containment: all three persisted state files (`lock_table.json`, `task_graph.json`,
`NodeStore`'s `metadata.json`) now write atomically and degrade per a policy
chosen for what losing each one actually costs, rather than raising out of a
constructor — a corrupt task graph used to break `--recover` on exactly the crash
it exists to handle. Per-request SDK timeouts closed a matching gap on the agent
side: no API adapter bounded its own call, so a wedged provider call defeated the
session's collect timeout (it stopped *waiting*, then `close()` blocked *joining*
that same call anyway); `AgentRunner` also gained the work dir it should have had
from the start, fixing CLI agents spawning in the process CWD and `--sandbox`
mounting the wrong tree into the container.

One correction happened *during* the wave, not after it, which is worth recording
because the process is the point. The first cut of the node-store gate rejected
any id containing `.mak/`, full stop — and broke the Wave 11 prune's own regression
test. It was right to: that prune exists to *delete* the `.mak/…` nodes an older
MAK ingested, and a store that refuses to **address** an id can never evict it.
Containment ("does this resolve outside the root?") and source policy ("is this
legitimate project source?") are different questions, and the store must only ever
answer the first — the fix split them into two parameters
(`mak_dir_name=None` opts out of the second) rather than one. A second correction
was a full withdrawal: the audit's claim that `AgentRunner._read_result` leaked a
reader thread on an agent timeout **does not hold** — every timeout path already
terminates the child, which closes the pipe and ends the blocked read in EOF, and a
thread-count probe confirmed the count returns to baseline. The attempted fix
(closing the pipe from the caller) was worse than the non-bug: `io.BufferedReader.
close()` contends for the same buffer lock the blocked reader holds, so it stalled
until the child died anyway — a 0.5s test timeout became 30s, and the full suite's
wall clock went from 8s to 38s before the regression was traced and reverted, with
a comment at the site recording why the pipe is deliberately left alone. Extending
`mypy --strict`/`ruff` to `cli/` (closing the gap that let the token-counter bug
below hide) found two more defects for free on the first run: a `Session._planner`
access with no `None` guard, and a `SimpleNamespace` passed where `build_session`
is typed to expect a real `argparse.Namespace` — both latent, both fixed. Finally,
the TUI's token counter was rewritten off three SDK monkeypatches: they hooked
`Messages.create` while both the Anthropic agent adapter and the Anthropic planner
call `messages.stream` (forced by the output budget), so the counter had been
reporting a flat zero for MAK's default provider, undetected because the old test
suite exercised only the pure per-provider helpers and never the patch point
itself. The gates closed the wave at 1216 tests, `mypy --strict` and `ruff` clean
over `mak` **and** `cli`.

**Wave 18 — residual hardening & hygiene.** The same audit's remaining findings,
closed so nothing is left outstanding: four residual **Medium** items and every
**Low/polish** one. Unlike Wave 17 these are largely independent of each other,
so the wave is grouped by area rather than by severity, and several are a few
lines each.

The two that change behaviour an operator can feel are the **no-op narrowing**
(§10) and the **spend ceiling** (§11). The first closes the last self-attested
completion: `no_changes_required` was still gated on an *existence* check rather
than a *work* check, so an agent that found a task hard could close it with one
boolean as long as the target file happened to be there and to parse. It is now
refused where the target cannot have existed to inspect — a file a `depends_on`
dependency created this wave, or a greenfield whole-file grant on the first
attempt — both decided from the plan, never from the agent's answer. The second
adds `session.max_total_tokens`, because nothing bounded a run's cost at all:
`max_attempts` × `max_iterations` × cascade waves × unbounded per-agent output
multiply out to no ceiling, and the per-wave approval prompt is a human gate, not
a budget.

The other two Mediums are pure cost, with the behaviour held fixed and asserted
that way. Enrichment's cross-file layer walked the whole store and regex-scanned
every node on **every dispatch and every retry** — the budget caps what is *sent*,
not what is *scanned* — and is now an inverted symbol index keyed on a store
generation counter (§3.2): ~8x per dispatch on this repo's own 892-node store,
byte-identical bundles, verified differentially against the implementation it
replaces. Ingestion globbed `**/*.py` over the entire tree and *then* discarded
`.venv`, `node_modules`, `site-packages` and `__pycache__`; it now prunes an
excluded directory before descending into it (§3.1): **496 ms → 12 ms** on this
repo for an identical file list, also verified differentially, across nine
pattern shapes and three exclusion sets.

The Lows are each small and each real. API keys are written with
`os.open(..., 0o600)` rather than written-then-`chmod`-ed, closing a window in
which the file sat at `0644` with the keys already in it, and the legacy in-package
`mak/.env` is deprecated with a warning naming its replacement (§11). `mak update`
resolves a release tag instead of tracking unpinned `HEAD`, and prints the version
it is moving to (§12.1.2). Two log events that misreported their own type —
a task *failure* and an agent-type *remap*, both logged as `TASK_COMPLETED` —
got the names they describe (§1). `list_nodes` sorts by `(file_path, order)`, so
the planner's inventory is no longer shuffled by a per-file `order` sorted
globally (§2). The node store gained a retention policy, deletes superseded
fragments' directories, and exposes `mak gc` for stores an older MAK left
unbounded (§2, §12.1.2). And `cli/runner.py`'s `while t.is_alive(): sleep(0.05)`
followed by `t.join()` is gone — it burned a core for the length of every run
and changed nothing about when the function returned.

One process note worth recording. `tests/conftest.py` is new, and it exists
because Wave 18's own deprecation warning broke an unrelated test: the suite read
the *developer's* real `mak/.env`, so what it asserted about stderr depended on
whose machine it ran on. Both `.env` lookups are now redirected to an empty temp
dir for every test. A test that passes because of a file outside the repo was
always going to fail eventually; it happened to be this wave that found it. The
gates closed at 1289 tests, `mypy --strict` and `ruff` clean over `mak` and `cli`.

**Wave 19** answered a second audit (2026-09-08), which found six P1 defects that
all shared one root: *consistency is not maintained across lifecycle boundaries*.
Every one was an operation that advanced part of the system's state and then either
failed, or was later contradicted by a second source of truth nobody reconciled.
What makes the wave worth recording is that **all 1,577 tests, `ruff`, and
`mypy --strict` were green while every one of them held** — the suite proved the
code did what it did, not what it claimed.

The six, and the boundary each failed to hold: a commit spanning store, disk, and
wave accounting could leave all three disagreeing (§2, §10); a later session
silently discarded human edits and kept reconstructing deleted symbols (§2, §10); a
task commit absorbed the user's unrelated staged work (§9); a cascade published a
green result over an earlier failure (§10); `teardown` reported "tests passed" when
no suite had run and pushed on it (§10); and two processes over one project both
granted a write lock on the same node (§4).

The fix was the same shape six times: **name the transaction, define its commit
point, make everything before it recoverable.** `NodeStore.transaction()` defers the
destructive effects and makes the metadata save the commit point; a write-ahead
journal makes an interrupted commit recoverable *by a later process*, deciding
roll-forward from roll-back by comparing recorded versions against the reopened
store. `sync_file` replaced one-directional ingestion with reconciliation in both
directions, and `retire_node` records a deletion without destroying its history —
the distinction `remove_node` could not express. Audit commits build in a private
`GIT_INDEX_FILE` seeded from HEAD, so the user's index is never opened at all.
`CascadeOutcome` and `ExecutionResult` separate *how much work succeeded* from
*whether the request was satisfied*. `SuiteOutcome` gave "no tests ran" a name
distinct from "the tests passed". And `ProjectLease` takes an OS-backed exclusive
lease before anything reads or mutates `.mak/` — which is also what finally made the
long-standing `lock_table.clear()` at startup sound, since holding the lease *is*
the proof the prior owner is dead.

Two process notes. Every item closed with a regression test written to fail against
the old code rather than merely exercise the new — `tests/test_wave19_acceptance.py`
has one class per numbered criterion from the report — and each of the durability
tests asserts against a **freshly reopened** store, because agreement between live
in-memory objects is exactly what the defects already had. The other is that this
wave's documentation pass had explicit instructions to *reduce* claims: the previous
text promised a transactional commit that "never diverges" and a teardown that
gates a push, and the code delivered neither. Those paragraphs now say what the
tests prove. The gates closed at 1672 tests, `mypy --strict mak cli` and
`ruff check mak cli tests` clean.

**Wave 20** is the first of the project's research track (TASKS.md's Waves
20–22 — evidence for the shared-memory thesis, not a user feature) and closes
the correctness gap the thesis has to survive first: node-level locks rule out
textual conflicts by construction, but PLANS §5.1 had listed a cycle-detection
check nothing implemented and named nine more semantic shapes with no check at
all, several of them invisible to the kernel for a *structural* reason —
only the planner's `context_nodes` were ever version-tracked, so anything
`_enrich_bundle` added on its own (three of the five layers, §3.2) could be
rewritten under a task with no way for the commit to know. Fourteen steps,
leaving the tree green after each: read-set versioning first (so every later
step has something to validate against), then the lock-resource split and the
planner-schema declarations it needs, the registrar module, stale-read
validation and its retry note, commit-time interface enforcement, six new
static checks, plan validation reading the declarations, declared-contract
dispatch, cascade rebuilt on the real reference graph, the four optional
gates, the seeded corpus, and finally the acceptance pass and this
documentation. Every step is a module or a session method with a name in
§4.5/§5.2/§8/§10/§11 above, so it is not repeated here; three decisions are
worth recording because they generalize past this wave. **A stale read's
identity is a content digest, never a version number** — a node that is
uncommitted or retired-and-recreated restarts at version 1 with different
content (the ABA problem a version-only check would miss silently).
**"Interface changed" means an existing binding was removed or re-bound, not
any text diff** — a node that only *gained* a name broke nobody, and the
old rule (any fingerprint diff) would have re-dispatched every task that
added a sibling helper next to the one it was reading. And **a commit that
cannot proceed *yet* is parked, never re-run**: re-dispatching a finished,
correct result to wait out a lock would spend a whole agent call and an
attempt of the retry budget to arrive at the same answer, so §10's parked-
commit machinery exists specifically to not do that, and the one place a
wait in this design can become a cycle (parking is the only state where a
task waits *while holding locks*) is broken the same way the deadlock
watchdog breaks any other cycle — release the youngest, let the rest
resolve. The corpus (§5.2) is the wave's own acceptance evidence: every one
of PLANS §5's nine representable shapes is prevented or detected under MAK,
against a git-worktree comparison run on the identical two edits, with zero
false positives on either side's single-edit runs and at most one extra agent
call for the shapes that need one. The gates closed at 2035 tests (plus three
pre-existing, unrelated `TestIterSourceFiles` failures — see Known
limitations — that this wave found already failing on `main` and left alone,
rather than fix something outside its own scope), `mypy --strict mak` and
`ruff check mak tests` clean.

**Wave 15** picked up the top-priority item Waves 17/18's security-and-robustness
audit had temporarily reordered around — local LLM support (§7.7, §8, §12.2, §14)
— and, like Wave 10 before it, is numbered for the roadmap item it closes rather
than for chronological order. Eighteen steps across four phases, each phase
leaving the tree green: **Phase A** built the transport and config core —
`local_api` and `ollama_api` as two agent types sharing one class where sharing
made sense and two where it didn't, the D2 key-never-leaked rule, one
`TaskResult` schema rendered in four provider dialects instead of the two
hand-copied ones that existed, and the parse→repair→retry loop both local
adapters use. **Phase B** added the piece an OpenAI-compatible transport cannot
provide by construction: a native Ollama client that reads a model's real
context window and sizes every request to it, refusing loudly instead of
letting the server truncate a bundle and answer from a fraction of the task.
**Phase C** made the interactive app usable with **no API key anywhere** — a
`mode` field, a first-run fork (cloud / local / hybrid), and a `/local` wizard
that detects, pulls, and confirms before ever touching `mak.yaml`. **Phase D**
shipped four packaged example configs, the packaging extras that let a
fully-local install skip every provider SDK, and an acceptance test
independent of the unit suite. One correction happened along the way: an
`AgentError` raised for a malformed OpenAI-mode reply had always been the
*wrong* exception (`AgentError` is `AgentResponseError`'s parent, so
`AgentRunner._assign_api`'s `except AgentResponseError` silently missed it),
which meant the schema-restating retry note Wave 12 built (§10) had never once
fired for that adapter — found while reading the adapter for this wave, not
caused by it, and fixed alongside the local-transport work rather than left for
a fifth pass at the same file. The gates closed at 1577 tests, `mypy --strict`
and `ruff` clean over `mak` and `cli`.

---

## Wave 21: zero-LLM simulated-agent scaling benchmark

Wave 21 adds `benchmark/sweep.py` and the `benchmark/sim/` package. A sweep
materializes a fresh synthetic Python target per arm, then drives the existing
benchmark runners with `SimBackend`. MAK still executes the production session,
lock table, node store, commit transaction, reconstruction, and conflict detector;
the worktree baselines still execute real git. The simulator substitutes only the
model-facing calls and uses a stable hash of `(seed, call kind, operation,
attempt)` as its random seed. This common-random-number design makes arm
comparisons paired instead of letting model-latency noise dominate them.

`harness/synthetic_spec.py` is the source of truth for generated stubs, reference
implementations, registration tables, dependency pairs, and oracle tests. It
supports uniform or Zipf table popularity, zero/one/two registrations per task,
commutative registry appends, exclusive same-node edits, same-file work, and
caller/callee pairs. Assignment can be module-owned, round-robin, random,
conflict-avoiding, or derived from a Wave 22 profile. Keep additions in that spec
so target code and the oracle cannot drift.

The worktree runner now supports `merge_at_end` and `merge_often` strategies and
reports registration survival after real merges. The MAK runner can serialize
file-sharing tasks for the file-granularity ablation. Both expose the expanded
`RunResult` metrics. `Session` emits `phase_span` events around commit validation
and reconstruction; the benchmark wraps the real lock table to measure wait
distributions without changing lock semantics.

Real provider calls write fitting telemetry when `MAK_BENCH_CALLS_PATH` is set.
Fit it with `benchmark/sim/fit.py`; profiles contain log-normal latency parameters,
an empirical bootstrap fallback, prompt-byte token regressions, and a Beta
posterior for resolver line drops. The bundled default is deliberately labeled as
a placeholder. Do not describe it as calibrated. `sim/calibrate.py` reports
prediction error when matching real points at N = 3, 6, and 10 are available.

The normal quick validation is:

```bash
python benchmark/sweep.py --config benchmark/sweeps/smoke.yaml
python benchmark/analysis/scaling.py \
  --input benchmark/results/simulated_agent_scaling_1_smoke.jsonl
python -m pytest tests/test_wave21_benchmark.py -q
```

Sweeps append resumable JSONL records keyed by every case parameter, the MAK git
SHA, and the profile hash. `--fresh` intentionally replaces one sweep's JSONL.
Each completed sweep also writes
`benchmark/simulated_agent_scaling_1_result.json`. Keep the report's real-versus-
modeled boundary explicit, preserve negative H1-H5 verdicts, and never infer real
model calibration from the keyless smoke data.

---

## Wave 22: Universal OpenAI-compatible endpoint abstraction

Before this wave, `AgentConfig.type` did three jobs at once: it selected which
adapter class to build, it *was* the wire protocol, and it *was* the registry's
routing key. That conflation made it impossible to point MAK at two
OpenAI-compatible services in one run — NVIDIA Build and OpenRouter both need
`type: "openai_api"`, so the second entry silently replaced the first in the
registry (§7.3) and one of the two configured agents simply never ran, with
nothing said about it. A hand-written `mak.yaml` pointing `base_url` at a
third-party gateway worked by accident; the CLI's `--models` flag and the
`/endpoint`-shaped features this wave adds did not exist at all. Wave 22 is
incomplete, by its own acceptance criterion, if any surface still says "Unknown
provider" for a service that speaks the OpenAI Chat Completions API.

**Four identities, previously one.** The fix separates **transport** (the wire
protocol an adapter speaks — `openai_chat`, `anthropic`, `ollama_native`, …),
**provider profile** (a named set of documented defaults — NVIDIA's URL, its
conventional key variable, what it's known to support), **endpoint** (one
configured, addressable service: a URL, a credential *reference*, capability
settings, all independent of any profile), and **agent id** (the routing key
every scheduler, planner, and log line uses). `type` now does exactly one job —
selecting a constructor — and every agent, endpoint-backed or legacy, resolves
through the same path via synthesized built-in endpoints for the three hosted
providers and the two local transports (`mak/endpoints/builtin.py`). `id`
carries the routing key `type` used to (§7.3); reserved ids (`anthropic`,
`openai`, `gemini`, `google`, `local`, `ollama`) keep `--models
<prefix>:<model>` unambiguous, since a legacy provider prefix and a user
endpoint id now share one namespace.

**The new package, `mak/endpoints/`.** `types.py` is the leaf: `Transport`,
`Location`, `ModelDiscovery`, `HealthPolicy`, `StructuredOutput`, and
`TokenParameter` as `StrEnum`s, id/env-name validation, and `EndpointConfig`
itself — no I/O, no dependency on `mak.config`, so every other module in the
package can depend on it without a cycle. `profiles.py` is the single source of
truth for the six shipped presets (nvidia, openrouter, deepseek, zai-general,
zai-coding, custom) — a dedicated test greps `mak/` and `cli/` for every preset
URL and key-env name and fails if either appears anywhere else, which is what
stops the CLI, the config parser, and an example config from each carrying a
copy that then drifts. `parse.py` turns YAML into an `EndpointConfig`,
deliberately **materializing identity while deferring capabilities** — it does
not bake a profile's capability defaults into the stored config, so a service
whose documented default MAK gets wrong someday can be corrected in
`profiles.py` for everyone still on `auto`, not just for entries written after
the fix. `resolution.py` performs the precedence walk at the moment an endpoint
is actually used: an explicit field on the endpoint beats the profile's
default, which beats the transport's own default. The tri-state distinction is
load-bearing here — an *unset* field (`None`) falls through the walk, but an
*explicit* `"none"` stops it, so a profile that defaults to `json_object` can
still be turned off for one endpoint without the walk picking the profile's
value back up underneath the override.

**Credentials are names, never values.** `api_key_env` names an environment
variable; MAK reads the key from it at resolution time and never stores, logs,
or forwards the value itself. An endpoint that names no variable is not treated
as "use whatever's ambient" — the resolution layer sends a non-secret
placeholder (`PLACEHOLDER_KEY = "local"`) instead, so an `OPENAI_API_KEY`
exported for the hosted OpenAI provider can never leak to an unrelated
`base_url` just because the SDK would otherwise pick it up by default. Extra
request headers follow the same rule (`EndpointHeaderConfig`, one of
`value`/`value_env`, never both) and MAK refuses an entry that tries to set
`Authorization`, `Content-Type`, `Host`, or `User-Agent` itself.

**The structured-output ladder now actually reaches its bottom rung.**
Pre-Wave-22 the adapter tracked a single boolean `downgraded`, so a service
rejecting both `json_schema` and `json_object` failed every task after using up
its one allowed descent — `json_schema → json_object` and then nowhere to go.
`capabilities.py`'s `rungs_from(mode)` now walks the full
`("json_schema", "json_object", "none")` ladder, and a session-scoped
`CapabilityCache` remembers which rung worked for each `(endpoint_id, model)`
pair — **injected into the adapter, not a module global**, so two sessions in
one process (or two tests) never share what one learned, and a lock protects
every read and write for the scheduler's concurrent dispatch. Tightening the
rejection detector was necessary to make the ladder trustworthy: it used to
match the bare word `"unsupported"` anywhere in an error body, which meant an
unsupported *model* or *region* was silently answered by stepping down the
reply's own structure — the wrong response to the wrong problem. It now
requires both a 400/422 status **and** a `response_format`/`json_schema`/`"json
mode"` marker in the body.

**Health is a policy, not an assumption.** `health.py` classifies failures
(credentials, not-found, rate-limited, incompatible, unreachable, SDK missing,
model missing, unknown) into one actionable line with secrets redacted, and
three explicit policies — `models` (list once), `chat` (a one-token probe, and
only when explicitly opted into, since it spends real money), `none` — replace
an assumption that health equals reachability. "Not probed" is a distinct state
from "healthy": a registry build must never make a network call for a builtin
cloud endpoint with no `base_url` just to decide whether to start.

**Model discovery and the manifest.** `sources_for_endpoints` builds a model
source per configured endpoint and fetches each in isolation — one endpoint's
failure never empties another's list, matching the guarantee the three
built-in providers already had (§13). The on-disk manifest moved to **schema
v2**, keyed by `(endpoint_id, model_id)` instead of `model_id` alone, migrated
forward automatically from v1; two services offering a model under the same id
are two independent entries now, never one silently overwriting the other.

**`/endpoint` and the config surface.** The interactive CLI gets a full
sub-command family — `add` (a gather-then-commit wizard: every question is
asked before anything is written, and a `CANCELLED` sentinel at any step
discards the whole draft), `list`, `show`, `edit`, `test`, `models`, `remove`,
`export` — documented in §12.2, and `mak.yaml` gets an `endpoints:` section
plus `id`/`endpoint` fields on `agents[]` and `planner`, documented in §11.
`--models` accepts a configured endpoint id in the provider position
(§12.1), and three or more OpenAI-compatible endpoints now run side by side in
one roster — the wave's acceptance test spins up three in-process fake HTTP
servers with different dialects (one rejecting `json_schema`, one with no
`/models` route, one requiring a specific bearer token) and drives a real run
across all three at once (`tests/support/fake_openai_server.py`,
`tests/test_wave22_acceptance.py`).

**Three deliberate behavior reversals**, each with its old test inverted and
cross-referenced to the new one: the registry now **errors** on a duplicate id
instead of silently overwriting (§7.3); a roster may now put **several models
on one provider** instead of being capped at one, with uniqueness enforced by
agent id instead (§12.1); and `save_keys` now **preserves** every name already
in `.env` instead of truncating to a fixed set of three (§12.2, API keys). A
`replace_factory` escape hatch was added to the registry for tests that
deliberately swap in a double, so the new duplicate-id error doesn't also
break legitimate test setup.

**Found along the way, not caused by the wave but surfaced by it:** the
endpoint store's failed-write cleanup could mask the real error behind a
`NotADirectoryError` from an unconditional `unlink`, now wrapped and reported
correctly; and a mid-implementation regression where the cloud path started
making a real network call at startup (closed by the `HEALTH_AUTO`
adapter-only default described above) would have broken every zero-config run
the moment health policies landed, caught by the existing bootstrap test suite
before it shipped. Two of the wave's own new tests briefly (in a since-reverted
local commit) called `monkeypatch.undo()` inside a test body — pytest shares
one `monkeypatch` instance with the autouse fixture that isolates `.env` file
I/O from the real one, so an `undo()` inside the test reverted that isolation
too. Both tests now save and restore `os.replace` by hand instead; a
pre-existing, unrelated instance of the same pattern in
`tests/test_wave19_acceptance.py` was noted as a follow-up rather than fixed
here, since it belongs to a different wave's test file.

The gates closed at 2528 passing tests, `ruff check mak cli tests` and `mypy
--strict mak cli` both clean. Three failures in
`tests/node_store/test_ingestion.py` were verified to fail identically on
`main` at the branch point and are unrelated to this wave's changes.

---

## Wave 24: Capability-aware OpenRouter structured-output negotiation

A user pointed MAK at `openrouter:inclusionai/ling-3.0-flash-vl:free`. The
planner worked — its request is ordinary text generation. Every *agent* task
failed, because MAK adds a `response_format` contract to get a `TaskResult`
back and that model's route does not implement the parameter. The session log
carried two refusals from the same OpenRouter/Novita route:

```text
model features structured outputs not support
model: inclusionai/ling-3.0-flash-vl does not support feature: structured-outputs
```

The 0.8.1 hotfix added the literal `"structured outputs"` to
`_FORMAT_REJECTION_MARKERS`. It matches the first spelling and misses the
second, so `_is_format_rejection()` returned false, the ladder never descended,
and the scheduler re-dispatched each failed task — amplifying one predictable
capability mismatch into repeated provider calls.

**Why extending the tuple was the wrong fix.** That sentence is written by
whichever upstream provider OpenRouter happened to route to. MAK does not get
to enumerate the spellings of a string it does not own, and the next provider
would have broken it again. Four deeper causes sat underneath the missed
substring: the catalog *discarded* the capability metadata OpenRouter publishes;
the endpoint policy was per-endpoint while support is per exact model variant;
MAK never told OpenRouter which parameters it needed honored; and several agents
starting together each ran the same failing probe ladder concurrently.

### What the live API actually said

Every design decision below was checked against the real service before it was
written down, because the wave's planning notes contained two assumptions that
turned out to be wrong.

`GET /api/v1/models` returns 446 models and publishes `supported_parameters` on
each. The incident model's two variants are opposites:

```text
inclusionai/ling-3.0-flash-vl        -> response_format ✓  structured_outputs ✓
inclusionai/ling-3.0-flash-vl:free   -> response_format ✗  structured_outputs ✗
```

**This is not one bad model.** 70 of the 446 publish no `response_format` at
all, and a further 30 publish `response_format` without `structured_outputs`.

**`response_format` and `structured_outputs` are two parameters, not one
feature.** `google/gemma-4-31b-it:free` publishes only the former. Probed
against it, `{"type": "json_object"}` routes fine and a strict `json_schema`
does not. So the mapping onto MAK's ladder is exact, and it is measured rather
than assumed:

| MAK rung | Reported parameter that authorizes it |
|---|---|
| `json_schema` | `structured_outputs` |
| `json_object` | `response_format` |
| `none` | — |

Gating the whole ladder on `response_format` — the obvious reading, and what
the wave originally planned — would have kept sending those 30 models a schema
they cannot honor, one wasted call per task, forever.

**An empty report means *unknown*, not *unsupported*.** Three catalog entries
(`openrouter/fusion`, `openrouter/pareto-code`, `openrouter/bodybuilder`, all
auto-routers) publish `supported_parameters: []`. Probing confirmed
`openrouter/fusion` **succeeds** with a strict schema. So only a *non-empty*
report that omits the parameter is a known negative; an empty one is a service
declining to enumerate.

**The routing guard fails with 404, outside the format window.** OpenRouter
documents combining `response_format` with `provider.require_parameters: true`
so routing only considers providers that honor the request. Its failure mode is
a **404** carrying `metadata.failed_routing_step: "Filter by Parameters"` — not
a 400 or 422. Sending it unconditionally, as originally planned, would
therefore have converted a *recoverable* provider rejection into a hard failure
the ladder's 400/422 gate could never descend from. The guard as designed would
have made MAK strictly less robust.

### The four pieces of the fix

**1. The catalog keeps capabilities (`mak/models/`).**
`OpenAiCompatibleSource` now reads `supported_parameters` — openai 3.16.2 keeps
unknown `/models` keys as pydantic extras, and `providers.py::reported_parameters`
is the single place that knows it, preferring `model_extra` so a future real SDK
field of that name cannot shadow the server's value. It flows through
`FetchedModel` → `ModelEntry` → manifest **schema v3**, serialized as a sorted
list for a byte-stable file and omitted entirely when unknown.

The field is **tri-state**, and all three states are load-bearing: `None` (the
service published nothing — every OpenAI/Anthropic/Gemini model and most
compatible `/models` routes), `frozenset()` (published and empty), and a
non-empty set. A boolean would conflate "known unsupported" with "unknown" and
silently disable structured output for every endpoint that lists bare ids.
Schema v1 and v2 records simply have no such key, which reads as `None`, so a
cache upgrades with no model loss and no refetch.

**2. Classification reads the error, not the string
(`mak/endpoints/error_classification.py`).**
A new leaf module, so the adapter keeps to transport and ladder control. It
parses in priority order — HTTP status, then `exc.body`, then
`response.json()`, then OpenRouter's `error.message`/`code`/`metadata`, then
`error.metadata.raw` (JSON-decoded when it is a JSON string, which is where the
useful sentence lives), with `str(exc)` last for compatibility. Text is
normalized to NFKC, casefolded, and every run of non-alphanumerics collapsed to
one space — which is what makes `structured outputs`, `structured-outputs` and
`structured_outputs` one token sequence matched by one marker.

A capability claim requires **all three**: a 400/422 status, a marker naming the
reply format, and language asserting an absent capability. An invalid JSON
Schema names the format but claims no missing capability, so it propagates —
hiding a MAK defect behind a quieter rung would be worse than the 400. So do
auth, missing model, quota, context limit, safety, transport failure and 5xx.
The verdict is a typed `RejectionAnalysis`, and it distinguishes a provider
format refusal from the routing-guard 404, because the two have different
recoveries.

**3. Catalog evidence picks the rung; runtime evidence wins.**
`CapabilityCache` now holds two kinds of fact in two fields, deliberately never
merged: *reported* parameters (a claim — it lowers the **starting** rung and
leaves descent below it available) and a *proven* mode (a real successful call —
it pins exactly). Merging them would let a stale catalog claim masquerade as a
verified fact, unfixable within the session. Both are clamped to what the user
configured, so neither a catalog nor another agent's discovery can raise an
agent above its own `structured_output` ceiling.

Seeding happens once at the composition root:
`ModelRegistry` → `ReportedCapabilities` → `bootstrap.seed_capabilities`. The
lookup is **injected** into `build_registry`, so that function stays pure, no
adapter factory reads the disk, and a test states what the catalog says without
writing a manifest. Only the reported set is seeded, never a mode — choosing
the rung belongs to the adapter, which is the only layer that knows the user's
ceiling.

Cache identity keeps the **whole** model id. `…-flash-vl` and
`…-flash-vl:free` are different products with opposite capabilities, so
canonicalizing a variant suffix away would attribute one's support to the other.

**4. The routing guard, sent only where the catalog confirms the rung.**
`provider_routing` is a typed policy (`ProviderRouting`) on the **profile**,
resolved through `ResolvedEndpoint` → `ResolvedAgentConfig` → `_api_factory()` →
adapter, and set on the `openrouter` preset alone. It is never inferred from a
hostname: a user can proxy OpenRouter, rename the endpoint, or point a `custom`
endpoint at the same domain, and a URL check answers wrongly in all three
cases. The `provider` object is added through the SDK's `extra_body`, so no
second SDK and no loosened typing for the ordinary parameters beside it.

It is sent only on a rung whose backing parameter the catalog *positively*
confirms — never on unknown, never on an empty report, never on the prompt-only
rung. And because OpenRouter's model-level aggregate can still disagree with the
endpoint it picks (measured: `gemma-4-31b-it:free` reports `response_format`
while its only route does not), a guard 404 **retries the same rung with the
guard dropped** rather than descending. Descending would surrender a capability
the model actually has.

**Single-flight discovery.** `CapabilityCache.discovering()` is a context
manager: the first caller for an unknown pair owns the ladder and the rest wait
on a per-key event, then start from what it proved. No network call happens
while the cache's lock is held; waiters are released on success **and** on
exception; a failed owner does not poison the key (the next caller becomes the
owner); keys are removed on completion so the cache stays bounded; and the wait
is bounded, so a wedged provider costs one agent its timeout rather than
stalling the pool. Before this, four agents starting together each paid for the
same two rejected formats.

### Invariants to preserve

- **Never widen the marker tuples to fix a new provider's wording.** If a
  refusal is not classified, the normalization or the priority-ordered parse is
  what needs work. A literal spelling is the thing this wave removed.
- **A rejection needs a status.** An exception with no HTTP status is never a
  rejection: a transport error has no opinion about the request body, even when
  a proxy echoed the request into its message.
- **Unknown, empty and reported are three states.** Any code that collapses
  them re-introduces the bug in one direction or the other. `None` must never
  become `frozenset()` on any hop.
- **Only positive confirmation gates the guard.** Its failure is a 404, so a
  speculative guard is a hard failure rather than a recoverable one.
- **A seed lowers, never raises.** The user's configured mode is a ceiling.
- **Raw bodies are read in memory and never logged.** Classification may
  inspect `metadata.raw`; only the short, `redact_secrets`-filtered `reason`
  reaches a log line, bounded to 200 characters. A provider body can echo
  request headers and user content, and these lines end up in shared issue
  reports.

### Debugging a structured-output problem

1. `INFO` logs one line per endpoint/model pair: the selected rung and the
   evidence (`catalog` or `runtime_rejection`). `catalog` means the endpoint
   published the limitation — look it up, it is a fact you can verify.
   `runtime_rejection` means MAK discovered it by being refused, which may be
   stale by tomorrow.
2. `DEBUG` logs a dropped routing guard, which is normal and self-healing: it
   costs one extra request and no capability.
3. To see what an endpoint claims:
   `curl -H "Authorization: Bearer $KEY" https://openrouter.ai/api/v1/models`
   and read `supported_parameters` for the **exact** id, suffix included. The
   per-model `…/models/<id>/endpoints` route shows it per upstream provider,
   which is where an aggregate and a route disagree.
4. `MAK_NO_MODEL_REFRESH=1` freezes the catalog, which separates "the seed is
   wrong" from "the runtime negotiation is wrong".

### Results

The acceptance case completes a real agent task against
`openrouter:inclusionai/ling-3.0-flash-vl:free` in **one** provider call with
no `response_format` and no `provider` object on the wire — verified live, not
only against a fake. With no catalog data at all the reactive ladder still
recovers within one dispatch, walking `json_schema → json_object → none`
against both real Novita spellings. A capable model keeps strict schema
enforcement and receives the guard.

The gates closed at 2683 passing tests — 109 new, including real-loopback-HTTP
acceptance over the actual openai SDK and sleep-free deterministic concurrency
tests — with `ruff check` and `mypy` clean. Four failures
(`tests/node_store/test_ingestion.py`, three parametrizations, plus
`tests/models/test_cli_adapter.py::test_every_entry_resolves_its_key_env`) were
verified to fail identically on `main` at the branch point.

**Found along the way, not fixed here.**
`ModelEntry.api_key_env` raises `ValueError` for any provider outside the
built-in three, so a catalog containing third-party endpoint entries breaks
`test_every_entry_resolves_its_key_env` — and `cli/commands.py` reads that
property. It reproduces on `main` and belongs to the model-catalog surface
rather than to capability negotiation, so it is recorded as a follow-up instead
of being folded into this wave.

---

# Part V — Design decisions & rationale

The decisions that shaped MAK, and why — useful when a change seems to cut against
the grain.

- **Shared memory over message passing.** Resolve conflicts at scheduling time
  (dependency graph explicit) rather than merge time (dependency graph lost).
- **Node store as source of truth; files are derived.** Enables symbol-level locking
  and position-independent identity, and means an agent only ever handles fragments.
- **LLM only in the planner.** Keep the runtime path deterministic; pay for language
  understanding exactly once, where it's genuinely needed.
- **Raw-source span tiling for ingestion** (not `ast.unparse`, not `libcst`).
  Comments, decorators, and formatting survive a round trip *by construction*, with
  zero extra dependencies. This superseded an earlier plan to adopt `libcst` once it
  became clear comments are lost at *ingestion*, before any reconstruction step runs.
- **API-first adapters with forced structured output.** Structured JSON is
  guaranteed, not scraped; the agent stays a constrained fragment-transformer rather
  than an autonomous file editor (which would bypass the node store and lock
  manager).
- **Thread-safe lock table via one table-wide re-entrant lock (option B).** Makes
  `try_acquire_all` genuinely atomic and is the prerequisite for real concurrency.
- **Human-in-the-loop plan review.** A ~5-second check eliminates the single point of
  failure in one-shot LLM DAG generation; bypassable with `--no-review`.
- **Transactional commit.** Validate the reassembled file before advancing the store,
  so the node store and disk never diverge. Wave 19 made this literal: one
  transaction spans the affected nodes, the superseded fragments, the output files,
  and the wave's accounting, with the store's metadata save as the single commit
  point and a write-ahead journal making everything before it recoverable by a
  *later process*. The earlier "best-effort revert" was not one — see §10.
- **`compile()` for validation, not `ast.parse()`.** `ast.parse()` is lenient about
  `from __future__` import placement: it accepts them anywhere in a file, even after
  regular code. Python's runtime and `compile()` both reject this with a
  `SyntaxError`. Every MAK validation gate (`_preview_is_valid`, conflict detector's
  parse gate, reconstruction guard, no-op acceptance) uses `compile()` so that the
  kernel rejects what Python would reject, rather than accepting source that passes
  the parser but fails the importer.
- **Sequential-first, concurrency-as-a-gated-wave.** Prove the pipeline end-to-end
  sequentially (Wave 4) before adding the thread pool (Wave 5). Don't add concurrency
  to a pipeline that has never run once — and don't claim the concurrent path works
  until its integration test is green.
- **A no-op requires positive assertion, never absence of evidence (Wave 12).**
  "The agent returned nothing" is not one condition — it's the collision of at
  least two: a genuine "nothing to change" and a reply truncated before it could
  say anything at all. Treating an absence as if it were a claim is what let a
  truncated response read as a successful no-op. The fix generalizes past this
  one case: whenever a positive and a negative outcome can produce
  indistinguishable evidence, require the positive case to assert itself
  explicitly rather than inferring it from the negative case's absence.
- **An assertion is only as good as what it could have been about (Wave 18).**
  The corollary to the above, and the reason the no-op guard needed narrowing
  twice. Requiring the agent to *assert* a no-op fixed the truncation collision
  but left an existence check standing in for a work check — the agent still
  awarded itself the completion, and the guard only asked whether the file
  happened to be there. What makes a self-assessment checkable is not the
  assertion's form but whether the thing assessed existed to be assessed, which
  is a question **the plan** can answer and the answer cannot.
- **Bound what a run scans, not only what it sends (Wave 18).** Two of this
  wave's costs hid behind correct filters: enrichment's byte budget capped what
  reached the agent while the scan behind it walked the whole store per dispatch,
  and ingestion's exclusion list was applied only after globbing everything it
  excluded. Both were invisible in output and expensive in practice. When a
  filter is downstream of the work, the filter is not the bound.
- **An event names what happened; a flag in a payload does not (Wave 18).**
  A failure logged as `TASK_COMPLETED(failed=True)` is legible only to a reader
  who knows to check the flag, so anything counting completions by type —
  including a human skimming — over-reported. Give the outcome its own name.
- **Prefer differential tests when the claim is "identical, only better".**
  Both of this wave's performance changes reimplement something subtle (glob
  matching; word-boundary regex semantics). Neither is trusted because it looks
  right: each is asserted equal to the implementation it replaces, over the real
  repo and a matrix of inputs, and the old implementation is kept in the test as
  the oracle.

---

# Glossary

- **Node** — the smallest independently lockable unit of code (a function, method,
  class shell, module header, or interstitial body block).
- **NodeId** — `<file>::<kind>::<qualified_name>`; position-independent identity.
- **Fragment (`NodeFragment`)** — a node's raw source text plus version/order
  metadata. The unit stored, dispatched, and reassembled.
- **TaskBundle / TaskResult** — the wire objects sent to / returned from an agent.
- **SubTask** — a planned unit of work with write targets, read context, and
  dependencies.
- **EditRound** — the set of staged fragments the conflict detector validates
  together — multi-task in a concurrent batch, so cross-agent conflicts are seen.
- **Adapter** — the swappable translator between MAK's protocol and a specific agent
  backend.
- **Composition root** — `mak/bootstrap.py`, which assembles configured collaborators
  from a `MakConfig`.
- **Wave** — a gated, parallelizable phase of the build-out (see Part IV).
- **`.mak/`** — the gitignored runtime directory (node store, lock table, task graph,
  session log).
- **`.makignore`** — the project-root, gitignore-style list of paths MAK never
  ingests; created with `.mak/` and `.git/` on first run (§3.1.1).

---

# License

[MIT](LICENSE) © 2026 Seungjoon Cha

By contributing, you agree that your contributions are licensed under the project's
MIT License.
