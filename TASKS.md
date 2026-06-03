# TASKS.md — 3-Agent Parallel Development Plan

> Subtasks derived from PLANS.md, organized into waves for 3 concurrent agents.
> Each wave must complete before the next begins. Within a wave, all 3 agents work in parallel.
> Every task includes its implementation files AND corresponding tests.

---

## Wave 0 — Foundation ✅ COMPLETE

Expand the core contracts and infrastructure that every downstream module imports.
Three independent foundation pieces that share no code dependencies.

**Status**: All 58 tests pass. `mypy --strict` clean. `pyyaml>=6.0` added to dependencies.

| Agent | Task | Files | Status |
|-------|------|-------|--------|
| A | Expand core types: added `NodeId` (NewType), `NodeFragment`, `LockEntry`, `TaskBundle`, `TaskResult`, `SubTask` frozen dataclasses. Added `INTENT_WRITE` to `LockMode`. Added exceptions: `NodeStoreError`, `PlannerFailedError`, `AgentError`, `UnknownAgentTypeError`. 16 tests for types, 6 for exceptions (incl. parametrized). Round-trip serialization via `dataclasses.asdict()` tested. | `mak/core/types.py`, `mak/core/exceptions.py`, `mak/core/__init__.py`, `tests/core/test_types.py`, `tests/core/test_exceptions.py` | ✅ Done |
| B | Created config module: `MakConfig` dataclass tree with `SessionConfig`, `PlannerConfig`, `AgentConfig`, `GitConfig`, `NodeStoreConfig` (all frozen, slots). `load_config()` loads YAML, validates required fields (agents must be non-empty, each agent needs `type`), applies defaults. Added `ConfigError` exception. 11 tests with YAML fixtures. | `mak/config.py`, `tests/test_config.py`, `mak/config.yaml`, `pyproject.toml` | ✅ Done |
| C | Created structured session logger: `EventType` StrEnum (8 event types), `LogEntry` frozen dataclass with `to_json()`/`from_json()` round-trip, `SessionLogger` with append-only JSON Lines writes, `read_log()`, `clear()`. 10 tests covering serialization, append behavior, directory creation, edge cases. | `mak/core/logging.py`, `tests/core/test_logging.py` | ✅ Done |

---

## Wave 1 — Three Core Subsystems ✅ COMPLETE

The three heaviest modules, all independent of each other. Each depends only on `core/types.py`.

**Status**: All 90 new tests pass (148 total). Round-trip ingestion test passes. All subsystems export through `__init__.py`.

| Agent | Task | Files | Status |
|-------|------|-------|--------|
| A | **Node Store**: (1) `ingestion.py` — `parse_file_into_fragments()` walks AST, extracts function/class/header/body nodes as `NodeFragment`s. `walk_and_parse()` recursively processes directories with include/exclude glob patterns. (2) `store.py` — `NodeStore` class with `get_node`, `put_node`, `commit_node`, `rollback_node`, `list_nodes`, `parse_file_into_nodes`, `get_committed_fragments`. Versioned fragments persisted to disk with `metadata.json` index. (3) `reconstruction.py` — `assemble_fragments()` sorts by kind order (header→class→function→body), `format_with_ruff()` auto-discovers venv ruff binary, `reconstruct_file()` assembles + validates + formats + writes. 12 ingestion tests, 12 store tests, 7 reconstruction tests including round-trip (ingest → store → reconstruct). | `mak/node_store/__init__.py`, `mak/node_store/store.py`, `mak/node_store/ingestion.py`, `mak/node_store/reconstruction.py`, `tests/node_store/__init__.py`, `tests/node_store/test_store.py`, `tests/node_store/test_ingestion.py`, `tests/node_store/test_reconstruction.py` | ✅ Done |
| B | **Lock Manager**: (1) `rwlock.py` — `RWLock` class with `can_acquire`, `acquire`, `release`, `holders`, `is_free`. Supports `read` (unlimited concurrent), `write` (exclusive), `intent_write` (compatible with reads). Same holder can escalate read→write. (2) `lock_table.py` — `LockTable` class: `try_acquire`, `try_acquire_all` (atomic multi-lock, all-or-nothing), `release`, `release_all`, `get_entries`, `get_holder_entries`, `all_entries`. Persists to/loads from JSON. Lock timeout expiration with configurable default (300s). (3) `deadlock_detector.py` — `DeadlockDetector` with `build_wait_graph()` (directed: waiter→holder with conflict check), `find_cycles()` (DFS), `resolve()` (wound-wait: abort youngest). 14 rwlock tests, 11 lock_table tests, 10 deadlock tests. | `mak/lock_manager/__init__.py`, `mak/lock_manager/rwlock.py`, `mak/lock_manager/lock_table.py`, `mak/lock_manager/deadlock_detector.py`, `tests/lock_manager/__init__.py`, `tests/lock_manager/test_rwlock.py`, `tests/lock_manager/test_lock_table.py`, `tests/lock_manager/test_deadlock_detector.py` | ✅ Done |
| C | **Agent Runner base**: (1) `protocol.py` — `encode_task_bundle`/`decode_task_bundle` and `encode_task_result`/`decode_task_result` with protocol version `"1.0"` validation, newline-delimited JSON. (2) `base_adapter.py` — `AgentAdapter` ABC with `spawn`, `format_task`, `parse_result`, `health_check` abstract methods. (3) `registry.py` — `ADAPTER_REGISTRY` dict, `register_adapter()`, `get_adapter()` with `UnknownAgentTypeError`, `list_adapters()`, `clear_registry()`. 11 protocol tests, 9 registry tests with stub adapter. | `mak/agent_runner/__init__.py`, `mak/agent_runner/protocol.py`, `mak/agent_runner/adapters/__init__.py`, `mak/agent_runner/adapters/base_adapter.py`, `mak/agent_runner/registry.py`, `tests/agent_runner/__init__.py`, `tests/agent_runner/test_protocol.py`, `tests/agent_runner/test_registry.py` | ✅ Done |

---

## Pre-Wave 2 Correction — libcst Migration ⚠️ SUPERSEDED BY WAVE H-A

**Funding condition #1.** The original scope (swap `normalize_with_ast()` for `libcst` at the
*reconstruction* step) is **insufficient** — `RISK_ASSESSMENT.md` C3 shows comments are lost at
**ingestion**, before reconstruction runs, so a reconstruction-only swap cannot preserve them. This
task is reframed and folded into **Wave H-A** as CST-based *ingestion* (comments attach to nodes at
parse time) plus the decorator/ordering/method fixes it depends on.

---

## Wave H — Hardening ✅ COMPLETE (gates Wave 2)

> Source: `RISK_ASSESSMENT.md` / `PLANS.md §15`. The "Complete" Wave 1 modules silently corrupted
> source (decorators stripped, top-level code reordered, comments dropped at ingestion, methods not
> lockable) and the lock layer had no defined concurrency model. All risks C1–C5, H1–H4, M1–M7,
> L1–L7 are addressed.
>
> **Status**: 199 tests pass (was 148; +51). `mypy --strict mak` clean. `ruff check mak tests`
> clean. The round-trip property test (PLANS §15.2) and concurrency stress test (§15.3) are green.
>
> **Resolved design decision — ingestion uses raw-source span tiling, not libcst.** The
> "CST-based ingestion" framing was superseded by a simpler, dependency-free approach: ingestion
> tiles a file into fragments that retain their *raw source text* and reconstruction concatenates
> them in original order. Because nothing is ever re-rendered through `ast.unparse()` or a CST,
> comments / decorators / formatting survive trivially. `libcst` is not a dependency. See
> PLANS.md §15.4.

| Agent | Task | Risk IDs | Files | Status |
|-------|------|----------|-------|--------|
| A | **Node store correctness + span-tiling ingestion.** ✅ (1) Decorator stripping fixed: spans computed from `min(decorator lineno)` → `end_lineno`. (2) Order preserved: fragments returned/stored in source order (`order` metadata), reconstruction concatenates in order — kind-bucket sort deleted. (3) Comments preserved via **raw-source span tiling** (fragments keep raw text; no `ast.unparse`/CST re-render) — superseded the libcst migration; dead `normalize_with_ast` deleted. (4) Method-level nodes: classes decompose into a `class` shell + `method` nodes (`file::method::Class.method`). (5) Duplicate ids disambiguated with `#n` suffix (overloads/conditional defs). (6) Round-trip property test added over a corpus (decorated defs, methods, constants between classes, top-level code between defs, inline + standalone comments, `@overload` stubs) + a test that MAK round-trips its own source. | C1 C2 C3 C4 C5 L6 | `mak/node_store/ingestion.py`, `mak/node_store/reconstruction.py`, `mak/node_store/store.py`, `tests/node_store/test_roundtrip.py`, `tests/node_store/test_ingestion.py`, `tests/node_store/test_reconstruction.py` | ✅ Done |
| B | **Lock manager concurrency model + versioning.** ✅ (1) Concurrency model = option B: `LockTable` and `NodeStore` mutations guarded by a re-entrant lock; `try_acquire_all` genuinely atomic. (2) Canonical conflict matrix in `conflicts.py` consumed by both `RWLock.can_acquire` and `DeadlockDetector`; `intent_write` now excludes writers. (3) Lease safety: `renew`/`renew_all` heartbeat + observable `expire_stale()` with `on_expire` callback and logging (no silent steal). (4) Versioning: `NodeStore` owns `current+1` on `put_node`, retains prior versions, `revert_node` rolls back to previous. (5) `find_cycles` iterative + deduplicated. (6) Concurrency stress test asserts no two conflicting holders ever coexist. | H1 H2 H3 H4 M7 L7 | `mak/lock_manager/rwlock.py`, `mak/lock_manager/lock_table.py`, `mak/lock_manager/deadlock_detector.py`, `mak/lock_manager/conflicts.py`, `mak/node_store/store.py`, `tests/lock_manager/test_concurrency.py`, `tests/lock_manager/test_lock_table.py`, `tests/lock_manager/test_rwlock.py`, `tests/lock_manager/test_deadlock_detector.py` | ✅ Done |
| C | **Gates, contracts & hygiene.** ✅ (1) `mypy --strict mak` clean (`lineno` access typed) and `ruff check mak tests` clean (isort first-party config; tests exempt from docstring rules). (2) CI (`.github/workflows/ci.yml`) + `.pre-commit-config.yaml` run ruff + mypy + pytest. (3) Wire protocol: `TaskBundle`/`TaskResult` are the single schema; `decode_task_bundle` rebuilds `LockEntry`/`ResourceRef`. (4) `AgentAdapter` split from `SubprocessAgentAdapter`. (5) Global registry → injectable `AdapterRegistry`. (6) Config parses `lock_timeout_s`/`deadlock_check_interval_s`; coercion wrapped in `ConfigError`; explicit string-bool. (7) `SessionLogger` serializes writes + flush. (8) `egg-info/` untracked + gitignored; design docs no longer gitignored. | M1 M2 M3 M4 M5 M6 L1 L2 L3 L4 L5 | `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `.gitignore`, `pyproject.toml`, `mak/agent_runner/*`, `mak/config.py`, `mak/config.yaml`, `mak/core/logging.py`, `tests/agent_runner/*`, `tests/test_config.py`, `tests/core/test_logging.py` | ✅ Done |

**Wave H completion criteria (all met):** round-trip property test green (decorators/order/comments/
methods intact); concurrency stress test green; `mypy --strict mak` clean; `ruff check mak tests`
clean; CI runs all three on push. **199 tests pass.** Wave 2 may now begin.

---

## Wave 2 — Dependent Subsystems 🔲 BLOCKED ON WAVE H

Each agent builds on exactly one Wave 1 module. No cross-dependencies within this wave.

| Agent | Task | Files | Depends On | Status |
|-------|------|-------|------------|--------|
| A | **Scheduler**: (1) `dag.py` — `DAG` class: construct directed graph from `SubTask.depends_on` edges, topological sort, `mark_complete(task_id)`, `newly_unblocked() → list[SubTask]`, cycle validation at construction time. (2) `scheduler.py` — `Scheduler` class: `tick()` loop that moves tasks from ready queue, calls `lock_manager.try_acquire_all()`, dispatches via `agent_runner.assign()`. `on_task_complete()` releases locks, marks DAG edges, extends ready queue. Persist DAG state to `.mak/task_graph.json`. Write tests for DAG operations and scheduler dispatch logic (mock lock_manager and agent_runner). | `mak/scheduler/__init__.py`, `mak/scheduler/dag.py`, `mak/scheduler/scheduler.py`, `tests/scheduler/__init__.py`, `tests/scheduler/test_dag.py`, `tests/scheduler/test_scheduler.py` | Wave 1-B (lock_manager) | 🔲 Not started |
| B | **Conflict Detector**: (1) `signature_check.py` — extract function signatures from AST, compare call sites against new signatures (arity, keyword args). (2) `import_check.py` — detect duplicate or conflicting imports across concurrent `__header__` edits. (3) `name_collision_check.py` — detect new symbols with same qualified name introduced by different agents in same file. (4) `detector.py` — `ConflictDetector` class orchestrating all checks, returns pass/fail with reasons. Write tests with crafted AST fragments that trigger each check. | `mak/conflict_detector/__init__.py`, `mak/conflict_detector/detector.py`, `mak/conflict_detector/signature_check.py`, `mak/conflict_detector/import_check.py`, `mak/conflict_detector/name_collision_check.py`, `tests/conflict_detector/__init__.py`, `tests/conflict_detector/test_signature_check.py`, `tests/conflict_detector/test_import_check.py`, `tests/conflict_detector/test_name_collision_check.py`, `tests/conflict_detector/test_detector.py` | Wave 1-A (node_store) | 🔲 Not started |
| C | **Agent Runner — API-first execution** (funding condition #2): (1) `runner.py` — `AgentRunner` class: manages API-based agent calls (primary) and subprocess pool for CLI adapters (secondary). `assign(adapter, task)` dispatches to API call or subprocess pipe depending on adapter type. Configurable timeout, discard-on-failure. (2) `anthropic_api_adapter.py` — **PRIMARY first adapter**: uses Anthropic Python SDK, sends TaskBundle JSON as user message, enforces `tool_use` or JSON response format to guarantee structured TaskResult output. No stdout scraping. (3) `openai_api_adapter.py` — OpenAI SDK adapter using JSON mode. Write tests with mocked SDK clients. | `mak/agent_runner/runner.py`, `mak/agent_runner/adapters/anthropic_api_adapter.py`, `mak/agent_runner/adapters/openai_api_adapter.py`, `tests/agent_runner/test_runner.py`, `tests/agent_runner/test_anthropic_api_adapter.py`, `tests/agent_runner/test_openai_api_adapter.py` | Wave 1-C (agent_runner base) | 🔲 Not started |

---

## Wave 3 — Higher-Level Modules

Planner, Git integration, and session orchestration. These wire the system together.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | **Planner + HitL DAG review**: `planner.py` — `Planner` class with `decompose(user_task, node_inventory) → list[SubTask]`. Build LLM prompt with node inventory (qualified names only). Parse JSON response, validate against `SubTask` schema, retry up to 3 times on parse failure, raise `PlannerFailedError` on exhaustion. **HitL step**: after decompose, call `display_plan_for_review(subtasks) → list[SubTask]` which prints the DAG to the terminal (task list + dependency edges) and prompts the user to approve, edit (JSON patch), or abort. Bypassed if `--no-review` flag set. Write tests with mocked LLM responses (valid JSON, malformed JSON, retry scenarios) and tests for HitL approve/edit/abort paths. | `mak/planner/__init__.py`, `mak/planner/planner.py`, `mak/planner/review.py`, `tests/planner/__init__.py`, `tests/planner/test_planner.py`, `tests/planner/test_review.py` | Wave 0 (core types) |
| B | **Git Integration**: `git.py` — `GitHelper` class: `commit_task(task_id, files, description, agent_type, session_id)` with MAK commit message format, `get_session_commits(session_id) → list[CommitInfo]`, `push(branch)`, `validate_clean_state()`. All git operations via `subprocess.run(["git", ...])`. Write tests using a temporary git repo fixture. | `mak/git_integration/__init__.py`, `mak/git_integration/git.py`, `tests/git_integration/__init__.py`, `tests/git_integration/test_git.py` | Wave 0 (core types) |
| C | **Session lifecycle + crash recovery + partial completion**: `session.py` — `Session` class orchestrating the full pipeline: init (ingest codebase into node store), run (planner → HitL review → scheduler loop → agent dispatch → conflict detection → commit), teardown (final test suite, push if green). Crash recovery: on startup, detect stale `.mak/lock_table.json`, release expired locks, re-queue incomplete tasks from `.mak/task_graph.json`. **Partial completion**: on `status: partial`, accept committed sub-fragments, mark only their node grants as complete, re-queue remaining grants as a narrower task. Add `SubTaskProgress` dataclass to track per-node-grant completion state. Write tests for session state machine transitions and partial completion paths. | `mak/session.py`, `tests/test_session.py` | Waves 1-A, 1-B, 2-A, 2-B, 2-C |

---

## Wave 4 — CLI, Security, & Integration Tests

Final polish. System should be functionally complete after this wave.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | **CLI entry point + integration tests**: `__main__.py` — `python -m mak --task "..."` entry point. Parse args (task description, config path, working dir, verbosity, `--no-review`, `--agent`). Initialize `Session`, run, handle errors with user-friendly messages. End-to-end integration test: ingest a small Python project containing inline comments, run MAK with a mock API agent, verify output files are correctly reconstructed **with all comments intact**. | `mak/__main__.py`, `tests/test_main.py`, `tests/test_integration.py` | Wave 3 (session) |
| B | **CLI adapters (secondary)**: `claude_code_adapter.py` — CLI adapter for `claude` CLI (secondary fallback). `codex_adapter.py` — adapter for `codex` CLI. Each implements `spawn`, `format_task`, `parse_result`, `health_check`. Write tests with mock subprocesses. Note: these are fallback options; API adapters (Wave 2-C) are primary. | `mak/agent_runner/adapters/claude_code_adapter.py`, `mak/agent_runner/adapters/codex_adapter.py`, `tests/agent_runner/test_claude_code_adapter.py`, `tests/agent_runner/test_codex_adapter.py` | Wave 1-C (agent_runner base) |
| C | **Agent sandboxing + config validation + error UX**: Docker-based process isolation for CLI-type agent subprocesses — scope filesystem access to working directory, restrict network (except approved API endpoints), add `--sandbox` flag to enable. `copilot_adapter.py` — adapter for `gh copilot` CLI. Config validation: verify `config.yaml` references only registered adapter types. Improve error messages across all modules. | `mak/agent_runner/sandbox.py`, `mak/agent_runner/adapters/copilot_adapter.py`, `tests/agent_runner/test_sandbox.py`, `tests/agent_runner/test_copilot_adapter.py`, `tests/test_config_validation.py` | Wave 1-C (agent_runner base) |

---

## Dependency Graph (Visual)

```
Wave 0:  [A: core/types]    [B: config]         [C: core/logging]
              │                                        │
              ├──────────────────┬─────────────────────┤
              ▼                  ▼                     ▼
Wave 1:  [A: node_store]   [B: lock_manager]    [C: agent_runner base]
              │                  │                     │
              ▼                  ▼                     ▼
Wave H:  [A: AST fixes +   [B: concurrency      [C: gates, CI,     ← 🔴 GATE: round-trip +
          CST ingestion]    model + versioning]  protocol, hygiene]   stress tests must pass
              │                  │                     │
              ▼                  ▼                     ▼
Wave 2:  [B: conflict_det] [A: scheduler]       [C: API adapters] ← funding condition #2
              │                  │                     │
              └──────────────────┼─────────────────────┘
                                 ▼
Wave 3:  [A: planner+HitL] [B: git integration] [C: session + partial completion]
                                                      │
              ┌──────────────────┬─────────────────────┤
              ▼                  ▼                     ▼
Wave 4:  [A: CLI + e2e]    [B: CLI adapters]    [C: sandboxing+validation]
```

---

## Agent Assignment Strategy

| Agent | Specialty Track | Rationale |
|-------|----------------|-----------|
| **A** | Data pipeline: types → node store → scheduler → planner → CLI | Owns the AST/data flow from ingestion to execution |
| **B** | Safety & correctness: config → lock manager → conflict detector → git | Owns concurrency safety and validation |
| **C** | Agent interface: logging → agent runner → adapters → session | Owns the subprocess boundary and orchestration |

Each agent builds expertise in its track across waves, reducing context-switching overhead.

---

## Completion Criteria per Wave

- **Wave 0**: ✅ `pytest tests/core/ tests/test_config.py` — 58 tests pass. `mypy --strict` clean. All shared types importable from `mak.core`.
- **Wave 1**: ✅ `pytest tests/node_store/ tests/lock_manager/ tests/agent_runner/test_protocol.py tests/agent_runner/test_registry.py` — 90 tests pass. Round-trip ingestion test passes. 148 total tests.
- **Pre-Wave 2**: `pytest tests/node_store/test_reconstruction.py` — comment-preservation test passes. `libcst` in dependencies. `reconstruction.py` uses `libcst`, not `ast.unparse()`.
- **Wave 2**: `pytest tests/scheduler/ tests/conflict_detector/ tests/agent_runner/test_runner.py tests/agent_runner/test_anthropic_api_adapter.py` all green. DAG topological sort verified. API adapter returns valid TaskResult from mocked SDK call.
- **Wave 3**: `pytest` full suite green. Session can initialize, ingest, plan (mocked LLM), display HitL review, dispatch (mocked agent), and tear down. Partial completion path tested.
- **Wave 4**: `python -m mak --task "..." --config mak/config.yaml` runs end-to-end with mock API agent. Reconstructed files contain original inline comments. `mypy --strict mak/` passes. `ruff check mak/` clean.
