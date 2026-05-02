# TASKS.md — 3-Agent Parallel Development Plan

> Subtasks derived from PLANS.md, organized into waves for 3 concurrent agents.
> Each wave must complete before the next begins. Within a wave, all 3 agents work in parallel.
> Every task includes its implementation files AND corresponding tests.

---

## Wave 0 — Foundation

Expand the core contracts and infrastructure that every downstream module imports.
Three independent foundation pieces that share no code dependencies.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | Expand core types: add `NodeId`, `NodeFragment`, `LockEntry`, `TaskBundle`, `TaskResult`, `SubTask` dataclasses. Add `intent_write` to `LockMode`. Add missing exceptions: `NodeStoreError`, `PlannerFailedError`, `AgentError`, `UnknownAgentTypeError`. Write tests for all new types and serialization contracts. | `mak/core/types.py`, `mak/core/exceptions.py`, `mak/core/__init__.py`, `tests/core/test_types.py`, `tests/core/test_exceptions.py` | — |
| B | Create config module: load `mak/config.yaml`, define `MakConfig` dataclass tree (session, planner, agents, git, node_store sections), validate required fields, provide defaults. Write tests with sample YAML fixtures. | `mak/config.py`, `tests/test_config.py`, `mak/config.yaml` | — |
| C | Create structured session logger: append-only JSON-lines log to `.mak/session.log`, event types (`task_started`, `task_completed`, `lock_acquired`, `lock_released`, `conflict_detected`, `agent_spawned`, `session_started`, `session_ended`), timestamp + event_type + payload schema. Write tests. | `mak/core/logging.py`, `tests/core/test_logging.py` | — |

---

## Wave 1 — Three Core Subsystems

The three heaviest modules, all independent of each other. Each depends only on `core/types.py`.

| Agent | Task | Files | Depends On |
|-------|------|-------|------------|
| A | **Node Store**: (1) `ingestion.py` — walk `.py` files, `ast.parse`, extract function/class/header/body nodes, write versioned fragments to `.mak/node_store/`. (2) `store.py` — `NodeStore` class with `get_node`, `put_node`, `commit_node`, `rollback_node`, `list_nodes`, `parse_file_into_nodes`. (3) `reconstruction.py` — assemble committed fragments in AST order, `ast.unparse()`, format with `subprocess.run(["ruff", "format", "-"])`, write to disk. Write tests for each file including round-trip (ingest → store → reconstruct == original). | `mak/node_store/__init__.py`, `mak/node_store/store.py`, `mak/node_store/ingestion.py`, `mak/node_store/reconstruction.py`, `tests/node_store/__init__.py`, `tests/node_store/test_store.py`, `tests/node_store/test_ingestion.py`, `tests/node_store/test_reconstruction.py` | Wave 0 (core types) |
| B | **Lock Manager**: (1) `rwlock.py` — per-node reader-writer lock primitive supporting `read`, `write`, `intent_write` modes. (2) `lock_table.py` — `LockTable` class: in-memory dict of `NodeId → LockEntry`, `try_acquire`, `try_acquire_all` (atomic multi-lock), `release`, `release_all`, persist to/load from `.mak/lock_table.json`, lock timeout expiration (default 300s). (3) `deadlock_detector.py` — build wait graph (directed: agent A waiting → agent B holding), DFS cycle detection, wound-wait resolution (abort youngest task). Write tests including concurrent lock scenarios and deadlock cycle detection. | `mak/lock_manager/__init__.py`, `mak/lock_manager/rwlock.py`, `mak/lock_manager/lock_table.py`, `mak/lock_manager/deadlock_detector.py`, `tests/lock_manager/__init__.py`, `tests/lock_manager/test_rwlock.py`, `tests/lock_manager/test_lock_table.py`, `tests/lock_manager/test_deadlock_detector.py` | Wave 0 (core types) |
| C | **Agent Runner base**: (1) `protocol.py` — `TaskBundle` and `TaskResult` JSON serialization/deserialization, protocol version validation, newline-delimited JSON encoding. (2) `base_adapter.py` — `AgentAdapter` ABC with `spawn`, `format_task`, `parse_result`, `health_check` abstract methods. (3) `registry.py` — `ADAPTER_REGISTRY` dict, `register_adapter()`, `get_adapter()` lookup with `UnknownAgentTypeError`. Write tests for protocol round-trips and registry behavior. | `mak/agent_runner/__init__.py`, `mak/agent_runner/protocol.py`, `mak/agent_runner/adapters/__init__.py`, `mak/agent_runner/adapters/base_adapter.py`, `mak/agent_runner/registry.py`, `tests/agent_runner/__init__.py`, `tests/agent_runner/test_protocol.py`, `tests/agent_runner/test_registry.py` | Wave 0 (core types) |

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

- **Wave 0**: `pytest tests/core/ tests/test_config.py` all green. All shared types importable.
- **Wave 1**: `pytest tests/node_store/ tests/lock_manager/ tests/agent_runner/test_protocol.py tests/agent_runner/test_registry.py` all green. Round-trip ingestion test passes.
- **Wave 2**: `pytest tests/scheduler/ tests/conflict_detector/ tests/agent_runner/test_runner.py` all green. DAG topological sort verified.
- **Wave 3**: `pytest` full suite green. Session can initialize, ingest, and tear down.
- **Wave 4**: `python -m mak --task "..." --config mak/config.yaml` runs end-to-end with mock agent. All adapters registered. `mypy --strict mak/` passes. `ruff check mak/` clean.
