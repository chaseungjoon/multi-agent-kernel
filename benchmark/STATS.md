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
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2** conflicted files, each an extra resolution call.

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

- **MAK** held node-level write locks on the shared `_register_all`, serializing only same-table registry edits: **0 conflicts**, **0 resolution calls**.
- **Traditional** merged 3 branches that all edited the shared `_register_all`: **2 conflicted files**, **2 resolution calls**.

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
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **2** conflicted files, each an extra resolution call.

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

- **MAK** held node-level write locks on the shared `_register_all`, serializing only same-table registry edits: **0 conflicts**, **0 resolution calls**.
- **Traditional** merged 3 branches that all edited the shared `_register_all`: **2 conflicted files**, **2 resolution calls**.

#### Traditional notes
- 5 agent-output note(s) across 10 runs (malformed/failed calls isolated per the parse gate; see per-run rows).

---

## Template 3 (real-world, 58 tasks)

- **Run at:** 2026-07-19T17:34:41 (mean of 10 runs)
- **Mode:** `real`
- **Agents:** 3 (claude-sonnet-5, gpt-5.6-sol, gemini-3.5-flash)
- **Workload:** 58 operations across 8 modules + 4 shared registry functions; 148 tests as the accuracy oracle.

> **Mode: `real`.** 3 agents (claude-sonnet-5, gpt-5.6-sol, gemini-3.5-flash) implementing 58 operations (verified by 148 tests). Figures are the **mean of 10 runs** (per-run breakdown below).

### Headline

| Metric | MAK | Traditional (worktrees) |
|---|---|---|
| Implementation time | 57.07s | 74.12s |
| Total tokens | 13,911 | 16,291 |
| Model calls | 44 | 44 |
| Accuracy (tests passed) | 111.4/148 (75%) | 93.7/148 (63%) |
| Registry merge conflicts | 0 | 4 |
| Conflict-resolution calls | 0 | 4 |

### Reading the numbers

- **Tokens:** MAK spent **15% fewer** (13,911 vs 16,291) — it reconciles nothing, so it makes no extra conflict-resolution calls.
- **Accuracy:** MAK higher — MAK 75% vs Traditional 63% (Traditional lost work in the merge).
- **Time:** MAK was faster (57.1s vs 74.1s) — contention is spread over 4 shared tables, so most tasks proceed in parallel while the worktree side still pays a sequential merge-and-resolve phase.
- **Coordination:** MAK hit **0** merge conflicts by construction; the worktree run hit **4** conflicted files, spread across the shared tables, each an extra resolution call.

### Token detail

| | MAK | Traditional |
|---|---|---|
| Input tokens | 6,843 | 8,969 |
| Output tokens | 7,068 | 7,322 |
| Total tokens | 13,911 | 16,291 |
| Model calls | 44 | 44 |

### Model calls per agent

| Agent | MAK | Traditional |
|---|---|---|
| agent0-claude-sonnet-5 | 22 | 26 |
| agent1-gpt-5.6-sol | 22 | 18 |
### Per-run breakdown (10 runs)

Each row is one independent run; the headline above is the mean of these.

| Run | MAK tokens | MAK passed | MAK time | Trad tokens | Trad passed | Trad time | Trad conflicts |
|---|---|---|---|---|---|---|---|
| 1 | 13,752 | 112/148 | 56.5s | 16,069 | 102/148 | 83.2s | 4 |
| 2 | 14,003 | 112/148 | 55.4s | 16,050 | 104/148 | 67.4s | 4 |
| 3 | 13,775 | 109/148 | 50.5s | 15,426 | 96/148 | 63.9s | 4 |
| 4 | 14,036 | 112/148 | 58.9s | 16,047 | 102/148 | 67.7s | 4 |
| 5 | 14,161 | 112/148 | 58.9s | 17,395 | 112/148 | 78.9s | 4 |
| 6 | 14,210 | 112/148 | 63.7s | 17,115 | 106/148 | 88.5s | 4 |
| 7 | 13,857 | 112/148 | 63.4s | 16,616 | 110/148 | 76.5s | 4 |
| 8 | 13,629 | 109/148 | 52.8s | 16,092 | 98/148 | 74.1s | 4 |
| 9 | 13,846 | 112/148 | 52.3s | 16,622 | 107/148 | 77.8s | 4 |
| 10 | 13,835 | 112/148 | 58.3s | 15,474 | 0/148 | 63.3s | 4 |


### Coordination

- **MAK** held node-level write locks on the 4 shared `_register_all` tables, serializing only same-table registry edits: **0 conflicts**, **0 resolution calls**.
- **Traditional** merged 3 branches that all edited the 4 shared `_register_all` tables: **4 conflicted files**, **4 resolution calls**.

#### MAK notes
- 10 agent-output note(s) across 10 runs (malformed/failed calls isolated per the parse gate; see per-run rows).

#### Traditional notes
- 179 agent-output note(s) across 10 runs (malformed/failed calls isolated per the parse gate; see per-run rows).
