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

Two targets, the same shape at two sizes — pick one with `--project basic|2|all`
(default `all`):

- **`basic`** (`project_template/`) — a `toolkit` library with **9 operations** across
  three modules (`strings`, `numbers`, `sequences`); a 30-test oracle.
- **`2`** (`project_template_2/`) — a much larger, harder `toolkit` with **90 operations**
  across nine modules (`strkit`, `numkit`, `seqkit`, `dictkit`, `datekit`, `mathkit`,
  `parsekit`, `setkit`, `codekit`) — utility functions in the spirit of
  `boltons`/`more-itertools`/`toolz` (Levenshtein distance, Roman numerals, calendar math,
  prime sieves, small parsers, set algebra, ciphers); a 270-test oracle. It is generated
  from `harness/template2_spec.py` by `tools/gen_template2.py`, so its stubs, reference
  implementations, and tests cannot drift.

Each is an unimplemented set of stubs plus a shared dispatch table,
`registry._register_all`, that **every** operation must add one line to — the whole
point: it is the one symbol every agent must touch. Under MAK, a node-level write lock
serializes those edits and none are lost. Under worktrees, every branch edits it
independently, so every merge after the first collides there and must be reconciled.
Module files are assigned one-agent-per-module, so they merge cleanly — the conflict is
isolated to exactly the contended symbol.

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
# Keyless self-test — proves the harness runs end to end (not representative numbers):
python benchmark/run_benchmark.py --mode mock

# The real benchmark — needs API keys for the models you choose (runs both targets):
export ANTHROPIC_API_KEY=sk-...   # and/or OPENAI_API_KEY, GEMINI_API_KEY
python benchmark/run_benchmark.py --mode real

# Just one target (the heavy one):
python benchmark/run_benchmark.py --mode real --project 2

# Average over several runs (the published Template 2 numbers are --repeat 10):
python benchmark/run_benchmark.py --mode real --project 2 --repeat 10

# Pick your own agents (provider:model), same set used for both sides:
python benchmark/run_benchmark.py --mode real \
  --models anthropic:claude-sonnet-4-6 openai:gpt-4o gemini:gemini-3-pro
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
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2**, each an extra resolution call.


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
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2**, each an extra resolution call.

See [STATS.md](STATS.md) for the full breakdown.

<!-- RESULTS:END -->
