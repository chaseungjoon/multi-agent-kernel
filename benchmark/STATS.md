# Benchmark results — detailed statistics

## Basic toolkit (9 ops)

- **Run at:** 2026-06-09T19:13:39
- **Mode:** `real`
- **Agents:** 3 (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6)
- **Workload:** 9 operations across 3 modules + 1 shared registry function; 30 tests as the accuracy oracle.

> **Mode: `real`.** 3 agents (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6) implementing 9 operations (verified by 30 tests).

### Headline

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 20.37s | 11.64s |
| Total tokens | 2,052 | 3,192 |
| Model calls | 9 | 11 |
| Accuracy (tests passed) | 30/30 (100%) | 30/30 (100%) |
| Registry merge conflicts | 0 | 2 |
| Conflict-resolution calls | 0 | 2 |

### Reading the numbers

- **Tokens:** MAK spent **36% fewer** (2,052 vs 3,192) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** tied at 100%. These tasks are small and the resolver merged the registry correctly *this time*; the structural risk MAK removes — a dropped or garbled registration — is what bites on larger tasks or weaker resolvers.
- **Time:** the worktree run was faster here (11.6s vs 20.4s): *every* task contends on the one shared registry node, so MAK serializes them while the worktrees run fully in parallel and reconcile afterwards. On a workload with more independent work, MAK parallelizes that part too — this benchmark deliberately maximizes contention.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2**, each an extra resolution call.

### Token detail

| | MAK | Traditional |
|---|---|---|
| Input tokens | 1,229 | 2,153 |
| Output tokens | 823 | 1,039 |
| Total tokens | 2,052 | 3,192 |
| Model calls | 9 | 11 |

### Model calls per agent

| Agent | MAK | Traditional |
|---|---|---|
| agent0-claude-sonnet-4-6 | 3 | 5 |
| agent1-claude-sonnet-4-6 | 3 | 3 |
| agent2-claude-sonnet-4-6 | 3 | 3 |

### Coordination

- **MAK** held a node-level write lock on the shared `_register_all`, serializing the 9 registry edits: **0 conflicts**, **0 resolution calls**.
- **Traditional** merged 3 branches that all edited `_register_all`: **2 conflicts**, **2 resolution calls**.

---

## Template 2 (90 ops)

- **Run at:** 2026-06-14T19:03:31 (mean of 10 runs)
- **Mode:** `real`
- **Agents:** 3 (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6)
- **Workload:** 90 operations across 9 modules + 1 shared registry function; 270 tests as the accuracy oracle.

> **Mode: `real`.** 3 agents (claude-sonnet-4-6, claude-sonnet-4-6, claude-sonnet-4-6) implementing 90 operations (verified by 270 tests). Figures are the **mean of 10 runs** (per-run breakdown below).

### Headline

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 226.54s | 99.52s |
| Total tokens | 18,339 | 23,760 |
| Model calls | 91 | 92 |
| Accuracy (tests passed) | 253.1/270 (94%) | 251.6/270 (93%) |
| Registry merge conflicts | 0 | 2 |
| Conflict-resolution calls | 0 | 2 |

### Reading the numbers

- **Tokens:** MAK spent **23% fewer** (18,339 vs 23,760) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** MAK higher — MAK 94% vs Traditional 93% (Traditional lost work in the merge).
- **Time:** the worktree run was faster here (99.5s vs 226.5s): *every* task contends on the one shared registry node, so MAK serializes them while the worktrees run fully in parallel and reconcile afterwards. On a workload with more independent work, MAK parallelizes that part too — this benchmark deliberately maximizes contention.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2**, each an extra resolution call.

### Token detail

| | MAK | Traditional |
|---|---|---|
| Input tokens | 10,378 | 13,481 |
| Output tokens | 7,961 | 10,279 |
| Total tokens | 18,339 | 23,760 |
| Model calls | 91 | 92 |

### Model calls per agent

| Agent | MAK | Traditional |
|---|---|---|
| agent0-claude-sonnet-4-6 | 30 | 32 |
| agent1-claude-sonnet-4-6 | 30 | 30 |
| agent2-claude-sonnet-4-6 | 30 | 30 |
### Per-run breakdown (10 runs)

Each row is one independent run; the headline above is the mean of these.

| Run | MAK tokens | MAK passed | MAK time | Trad tokens | Trad passed | Trad time | Trad conflicts |
|---|---|---|---|---|---|---|---|
| 1 | 18,163 | 253/270 | 213.4s | 23,737 | 253/270 | 90.1s | 2 |
| 2 | 18,129 | 253/270 | 225.2s | 23,796 | 253/270 | 94.4s | 2 |
| 3 | 18,588 | 253/270 | 235.9s | 23,697 | 250/270 | 108.5s | 2 |
| 4 | 18,457 | 253/270 | 224.4s | 23,761 | 250/270 | 97.0s | 2 |
| 5 | 18,209 | 254/270 | 211.1s | 23,879 | 247/270 | 93.2s | 2 |
| 6 | 18,221 | 253/270 | 224.8s | 23,724 | 250/270 | 97.6s | 2 |
| 7 | 18,695 | 253/270 | 242.5s | 23,746 | 253/270 | 103.6s | 2 |
| 8 | 18,381 | 253/270 | 253.2s | 23,735 | 253/270 | 108.9s | 2 |
| 9 | 18,137 | 253/270 | 216.7s | 23,749 | 254/270 | 102.7s | 2 |
| 10 | 18,408 | 253/270 | 218.1s | 23,782 | 253/270 | 99.1s | 2 |


### Coordination

- **MAK** held a node-level write lock on the shared `_register_all`, serializing the 90 registry edits: **0 conflicts**, **0 resolution calls**.
- **Traditional** merged 3 branches that all edited `_register_all`: **2 conflicts**, **2 resolution calls**.

#### Traditional notes
- 5 agent-output note(s) across 10 runs (malformed/failed calls isolated per the parse gate; see per-run rows).
