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

## Wave 2 — Dependent Subsystems

Each agent builds on exactly one Wave 1 module. No cross-dependencies within this wave.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | **Scheduler**: (1) `dag.py` — `DAG` class: construct directed graph from `SubTask.depends_on` edges, topological sort, `mark_complete(task_id)`, `newly_unblocked() → list[SubTask]`, cycle validation at construction time. (2) `scheduler.py` — `Scheduler` class: `tick()` loop that moves tasks from ready queue, calls `lock_manager.try_acquire_all()`, dispatches via `agent_runner.assign()`. `on_task_complete()` releases locks, marks DAG edges, extends ready queue. Persist DAG state to `.mak/task_graph.json`. Write tests for DAG operations and scheduler dispatch logic (mock lock_manager and agent_runner). | `mak/scheduler/__init__.py`, `mak/scheduler/dag.py`, `mak/scheduler/scheduler.py`, `tests/scheduler/__init__.py`, `tests/scheduler/test_dag.py`, `tests/scheduler/test_scheduler.py` | Wave 1-B (lock_manager) |
| B | **Conflict Detector**: (1) `signature_check.py` — extract function signatures from AST, compare call sites against new signatures (arity, keyword args). (2) `import_check.py` — detect duplicate or conflicting imports across concurrent `__header__` edits. (3) `name_collision_check.py` — detect new symbols with same qualified name introduced by different agents in same file. (4) `detector.py` — `ConflictDetector` class orchestrating all checks, returns pass/fail with reasons. Write tests with crafted AST fragments that trigger each check. | `mak/conflict_detector/__init__.py`, `mak/conflict_detector/detector.py`, `mak/conflict_detector/signature_check.py`, `mak/conflict_detector/import_check.py`, `mak/conflict_detector/name_collision_check.py`, `tests/conflict_detector/__init__.py`, `tests/conflict_detector/test_signature_check.py`, `tests/conflict_detector/test_import_check.py`, `tests/conflict_detector/test_name_collision_check.py`, `tests/conflict_detector/test_detector.py` | Wave 1-A (node_store) |
| C | **Agent Runner execution**: (1) `runner.py` — `AgentRunner` class: subprocess pool (configurable size per agent type), `spawn_agent()`, `assign(adapter, task)` writes TaskBundle JSON to stdin, reads TaskResult JSON from stdout with timeout, `SIGTERM` on timeout, idle pool management, discard-on-failure. (2) `claude_code_adapter.py` — first concrete `AgentAdapter`: spawns `claude` CLI, formats TaskBundle for Claude Code's stdin, parses Claude Code's stdout into TaskResult. Write tests with mock subprocess for runner lifecycle and a stub adapter for integration. | `mak/agent_runner/runner.py`, `mak/agent_runner/adapters/claude_code_adapter.py`, `tests/agent_runner/test_runner.py`, `tests/agent_runner/test_claude_code_adapter.py` | Wave 1-C (agent_runner base) |

---

## Wave 3 — Higher-Level Modules

Planner, Git integration, and session orchestration. These wire the system together.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | **Planner**: `planner.py` — `Planner` class with `decompose(user_task, node_inventory) → list[SubTask]`. Build LLM prompt with node inventory (qualified names only). Parse JSON response, validate against `SubTask` schema, retry up to 3 times on parse failure, raise `PlannerFailedError` on exhaustion. Support bypass with hardcoded task templates for known patterns. Write tests with mocked LLM responses (valid JSON, malformed JSON, retry scenarios). | `mak/planner/__init__.py`, `mak/planner/planner.py`, `tests/planner/__init__.py`, `tests/planner/test_planner.py` | Wave 0 (core types) |
| B | **Git Integration**: `git.py` — `GitHelper` class: `commit_task(task_id, files, description, agent_type, session_id)` with MAK commit message format, `get_session_commits(session_id) → list[CommitInfo]`, `push(branch)`, `validate_clean_state()`. All git operations via `subprocess.run(["git", ...])`. Write tests using a temporary git repo fixture. | `mak/git_integration/__init__.py`, `mak/git_integration/git.py`, `tests/git_integration/__init__.py`, `tests/git_integration/test_git.py` | Wave 0 (core types) |
| C | **Session lifecycle + crash recovery**: `session.py` — `Session` class orchestrating the full pipeline: init (ingest codebase into node store), run (planner → scheduler loop → agent dispatch → conflict detection → commit), teardown (final test suite, push if green). Crash recovery: on startup, detect stale `.mak/lock_table.json`, release expired locks, re-queue incomplete tasks from `.mak/task_graph.json`. Write tests for session state machine transitions. | `mak/session.py`, `tests/test_session.py` | Waves 1-A, 1-B, 2-A, 2-B, 2-C |

---

## Wave 4 — CLI, Adapters, & Integration Tests

Final polish. System should be functionally complete after this wave.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | **CLI entry point**: `__main__.py` — `python -m mak --task "..."` entry point. Parse args (task description, config path, working dir, verbosity). Initialize `Session`, run, handle errors with user-friendly messages. Write end-to-end integration tests: ingest a small Python project, run MAK with a mock agent, verify output files are correctly reconstructed. | `mak/__main__.py`, `tests/test_main.py`, `tests/test_integration.py` | Wave 3 (session) |
| B | **Codex + Gemini adapters**: `codex_adapter.py` — adapter for `codex` CLI. `gemini_adapter.py` — adapter for `gemini` CLI. Each implements `spawn`, `format_task`, `parse_result`, `health_check`. Write tests with mock subprocesses. | `mak/agent_runner/adapters/codex_adapter.py`, `mak/agent_runner/adapters/gemini_adapter.py`, `tests/agent_runner/test_codex_adapter.py`, `tests/agent_runner/test_gemini_adapter.py` | Wave 1-C (agent_runner base) |
| C | **Copilot adapter + config validation + error UX**: `copilot_adapter.py` — adapter for `gh copilot` CLI. Config validation: verify `config.yaml` references only registered adapter types, validate path existence, check CLI tool availability. Improve error messages across all modules. Write tests. | `mak/agent_runner/adapters/copilot_adapter.py`, `tests/agent_runner/test_copilot_adapter.py`, `tests/test_config_validation.py` | Wave 1-C (agent_runner base) |

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
Wave 2:  [B: conflict_det] [A: scheduler]       [C: runner + claude adapter]
              │                  │                     │
              └──────────────────┼─────────────────────┘
                                 ▼
Wave 3:  [A: planner]      [B: git integration] [C: session + crash recovery]
                                                      │
              ┌──────────────────┬─────────────────────┤
              ▼                  ▼                     ▼
Wave 4:  [A: CLI + e2e]    [B: codex+gemini]    [C: copilot+validation]
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
- **Wave 2**: `pytest tests/scheduler/ tests/conflict_detector/ tests/agent_runner/test_runner.py` all green. DAG topological sort verified.
- **Wave 3**: `pytest` full suite green. Session can initialize, ingest, and tear down.
- **Wave 4**: `python -m mak --task "..." --config mak/config.yaml` runs end-to-end with mock agent. All adapters registered. `mypy --strict mak/` passes. `ruff check mak/` clean.
