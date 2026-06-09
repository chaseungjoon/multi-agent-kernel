# Benchmark results — detailed statistics

- **Run at:** 2026-06-09T19:13:39
- **Mode:** `real`
- **Agents:** 3 (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6)
- **Workload:** 9 operations across 3 modules + 1 shared registry function; 30 tests as the accuracy oracle.

> **Mode: `real`.** 3 agents (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6) implementing 9 operations (verified by 30 tests).

## Headline

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 20.37s | 11.64s |
| Total tokens | 2,052 | 3,192 |
| Model calls | 9 | 11 |
| Accuracy (tests passed) | 30/30 (100%) | 30/30 (100%) |
| Registry merge conflicts | 0 | 2 |
| Conflict-resolution calls | 0 | 2 |

## Reading the numbers

- **Tokens:** MAK spent **36% fewer** (2,052 vs 3,192) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** tied at 100%. These tasks are small and the resolver merged the registry correctly *this time*; the structural risk MAK removes — a dropped or garbled registration — is what bites on larger tasks or weaker resolvers.
- **Time:** the worktree run was faster here (11.6s vs 20.4s): *every* task contends on the one shared registry node, so MAK serializes them while the worktrees run fully in parallel and reconcile afterwards. On a workload with more independent work, MAK parallelizes that part too — this benchmark deliberately maximizes contention.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2**, each an extra resolution call.

## Token detail

| | MAK | Traditional |
|---|---|---|
| Input tokens | 1,229 | 2,153 |
| Output tokens | 823 | 1,039 |
| Total tokens | 2,052 | 3,192 |
| Model calls | 9 | 11 |

## Model calls per agent

| Agent | MAK | Traditional |
|---|---|---|
| agent0-claude-sonnet-4-6 | 3 | 5 |
| agent1-claude-sonnet-4-6 | 3 | 3 |
| agent2-claude-sonnet-4-6 | 3 | 3 |

## Coordination

- **MAK** held a node-level write lock on the shared `_register_all`, serializing the 9 registry edits: **0 conflicts**, **0 resolution calls**.
- **Traditional** merged 3 branches that all edited `_register_all`: **2 conflicts**, **2 resolution calls**.
