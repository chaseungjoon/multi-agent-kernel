# PLANS.md — Multi Agent Kernel: Implementation Plan

> This document is the authoritative technical design reference for MAK.
> It covers architecture decisions, data structures, concurrency model, agent interface,
> and implementation roadmap. Update this document when design decisions change.

---

## 0. Problem Statement

Existing multi-agent coding systems use Git worktrees as their isolation primitive —
each agent works on a separate branch, and conflicts are resolved at merge time.
This is message-passing architecture: agents are decoupled, work independently,
and synchronize only at boundaries.

MAK replaces this with a shared-memory model. All agents operate on a single working
directory. The MAK kernel owns a node-level lock table and a symbol-versioned store,
and arbitrates concurrent access the way an OS arbitrates shared memory between threads.
Git is not used for isolation. It is used only as a post-hoc audit log, written by
agents themselves after MAK validates their output.

**Core constraint**: MAK must be self-contained and bootstrap-capable. No external
orchestration system. The kernel manages everything: planning, scheduling, lock
arbitration, agent lifecycle, conflict detection, and file reconstruction.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                            MAK KERNEL                               │
│                                                                     │
│  ┌─────────────┐    ┌──────────────────┐    ┌───────────────────┐   │
│  │   Planner   │───▶│ Dependency Graph │───▶│    Scheduler      │   │
│  │  (LLM call) │    │    Builder       │    │  (DAG traversal)  │   │
│  └─────────────┘    └──────────────────┘    └────────┬──────────┘   │
│                                                      │              │
│  ┌───────────────────────────────────────────────────▼──────────┐   │
│  │                      Lock Manager                            │   │
│  │   node_id → { holder, mode, acquired_at, dependencies }      │   │
│  └───────────────────────────────────────────────────┬──────────┘   │
│                                                      │              │
│  ┌───────────────────────────────────────────────────▼──────────┐   │
│  │                      Node Store                              │   │
│  │   (file_path, symbol_type, qualified_name) → versioned AST   │   │
│  └───────────────────────────────────────────────────┬──────────┘   │
│                                                      │              │
│  ┌───────────────────────────────────────────────────▼──────────┐   │
│  │                   Conflict Detector                          │   │
│  │        AST re-parse → semantic validation → rollback         │   │
│  └───────────────────────────────────────────────────┬──────────┘   │
│                                                      │              │
│  ┌───────────────────────────────────────────────────▼──────────┐   │
│  │                    Agent Runner                              │   │
│  │    spawn subprocess → assign task → collect output           │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
   subprocess: claude    subprocess: codex    subprocess: gemini
   (AgentAdapter)        (AgentAdapter)       (AgentAdapter)
          │                    │                    │
          └────────────────────┼────────────────────┘
                               ▼
                    Shared Working Directory
                    + Node Store (on disk)
                    + Git (audit log only)
```

**The kernel is a single Python process.** Agents are independent subprocesses —
CLI tools (Claude Code, Codex, Gemini CLI, Copilot, etc.) spawned and managed by
the Agent Runner. The kernel communicates with agents via structured stdin/stdout
protocol over the subprocess pipe. Agents are fully swappable; the kernel never
calls agent-specific APIs directly.

---

## 2. Source of Truth: The Node Store

### 2.1 Concept

The Node Store is MAK's equivalent of shared memory. It replaces the filesystem
as the source of truth for code. Files on disk are **derived artifacts** —
reconstructed from the node store on demand. The node store is what agents
read from and write to.

A **node** is the smallest independently lockable unit of code. For Python:

| Node Type | Example | Identity Key |
|---|---|---|
| `function` | `def foo():` | `(file, "function", "ClassName.method_name")` |
| `class` | `class Foo:` | `(file, "class", "ClassName")` |
| `module_header` | imports + module-level constants | `(file, "module_header", "__header__")` |
| `module_body` | module-level code outside functions/classes | `(file, "module_body", "__body__")` |

Node identity is **position-independent** — based on qualified name, not line number.
This means a new function being inserted by Agent A does not invalidate Agent B's
lock on an existing function in the same file.

### 2.2 On-Disk Layout

```
.mak/
├── node_store/
│   └── mak/
│       └── scheduler/
│           └── dag.py/
│               ├── __header__.v1.py        ← module header, version 1
│               ├── TopologicalSorter.v1.py ← class node, version 1
│               └── TopologicalSorter.sort.v1.py  ← method node, version 1
├── lock_table.json     ← persisted lock state (rebuilt on crash recovery)
├── task_graph.json     ← current DAG of pending tasks
└── session.log         ← append-only event log
```

Version files are plain Python fragments — valid Python that can be parsed in
isolation. They are what agents receive as context and what they return as output.

### 2.3 Node Store Operations

```python
# mak/node_store/store.py

class NodeStore:
    def get_node(self, node_id: NodeId) -> NodeFragment
    def put_node(self, node_id: NodeId, fragment: NodeFragment, version: int) -> None
    def commit_node(self, node_id: NodeId, version: int) -> None
    def rollback_node(self, node_id: NodeId, version: int) -> None
    def reconstruct_file(self, file_path: str) -> str   # assembles all nodes → file
    def list_nodes(self, file_path: str) -> list[NodeId]
    def parse_file_into_nodes(self, file_path: str) -> list[NodeId]
```

`reconstruct_file` assembles all committed node versions for a given file in
correct AST order, calls `ast.unparse()`, then runs `ruff format` to restore
style. The result is written to disk.

---

## 3. Lock Manager

### 3.1 Lock Model

MAK uses a **reader-writer lock per node**, with the following modes:

| Mode | Concurrent holders | Use case |
|---|---|---|
| `read` | unlimited | agent reads symbol as context |
| `write` | 1 (exclusive) | agent edits symbol |
| `intent_write` | multiple | agent declares future write (used for deadlock prevention) |

Lock requests are queued. An agent blocks (its task is held in the scheduler's
pending queue) if the required write lock is already held. The lock manager
never grants conflicting locks.

### 3.2 Lock Table Schema

```python
# mak/lock_manager/types.py

@dataclass
class LockEntry:
    node_id: NodeId
    mode: Literal["read", "write", "intent_write"]
    holder: str          # agent_id
    task_id: str
    acquired_at: float   # unix timestamp
    timeout_s: float     # lock expires after this; default 300s
```

The lock table lives in memory during a session and is persisted to
`.mak/lock_table.json` after every mutation for crash recovery.

### 3.3 Deadlock Detection

MAK runs a cycle-detection pass over the **lock wait graph** every 5 seconds.
The wait graph is a directed graph: edge A → B means agent A is waiting for a
lock held by agent B. A cycle in this graph is a deadlock.

Resolution strategy: abort the **youngest task** in the cycle (lowest priority
or most recently started), release its locks, and re-queue it. This is the
same wound-wait strategy used in database transaction managers.

```python
# mak/lock_manager/deadlock_detector.py

class DeadlockDetector:
    def build_wait_graph(self, lock_table: LockTable, wait_queue: WaitQueue) -> nx.DiGraph
    def find_cycles(self, graph: nx.DiGraph) -> list[list[str]]   # returns agent_id cycles
    def resolve(self, cycle: list[str], scheduler: Scheduler) -> str  # returns aborted agent_id
```

`networkx` is used for graph operations. If avoiding the dependency matters,
a simple DFS cycle detector is ~30 lines and trivially replaceable.

---

## 4. AST Pipeline

This is the kernel's core technical mechanism. It replaces Git's diff/merge
with a structured, semantic operation.

### 4.1 Ingestion (File → Node Store)

When MAK initializes on an existing codebase:

```
1. Walk the working directory, find all .py files
2. For each file:
   a. ast.parse(source)
   b. Walk the AST; identify top-level function/class nodes and their methods
   c. Extract each node's source range using ast.get_source_segment()
   d. Write each node as a versioned fragment to the node store
   e. Write the module header (imports + module-level statements) as __header__
3. Record the full node inventory in .mak/task_graph.json
```

### 4.2 Fragment Dispatch (Node Store → Agent)

When an agent is assigned a task:

```
1. Collect the agent's write-locked nodes + read-locked context nodes
2. Assemble a TaskBundle:
   - write_fragments: list of node fragments the agent may edit
   - read_context: list of node fragments the agent may read (signatures only
     for large classes; full body for small ones)
   - task_description: natural language instruction
   - style_guide: extracted from AGENTS.md
3. Serialize TaskBundle to JSON and write to agent's stdin
```

The agent only sees its fragment — it never sees the full file. This is the
shared-memory model: agents see a window into the codebase, not the codebase.

### 4.3 Collection (Agent Output → Node Store)

When an agent completes its task:

```
1. Read agent's stdout: expect a JSON TaskResult
2. For each modified fragment in TaskResult.write_fragments:
   a. ast.parse(fragment) — if this fails, reject immediately and rollback
   b. Validate that the fragment contains only the symbols the agent was
      authorized to modify (compare AST node names against lock grants)
   c. Write the new fragment as version N+1 in the node store (not committed yet)
3. Run cross-node validation (see Conflict Detector, section 5)
4. If all checks pass: commit all pending versions, release locks
5. If any check fails: rollback all pending versions for this task, re-queue task
```

### 4.4 Reconstruction (Node Store → File)

After a set of nodes is committed:

```
1. Collect all committed node versions for the file
2. Sort by original AST position (preserved as metadata in node store)
3. Concatenate fragments (libcst preserves all inline comments and whitespace)
4. libcst.parse_module() the assembled source — must succeed
5. libcst.Module.code to emit normalized source (comments intact)
6. ruff format to restore style
7. Write to disk
8. Agent commits to Git (see section 7)
```

**Architecture decision (resolved):** MAK uses `libcst` — not `ast.unparse()` — for reconstruction.
`ast.unparse()` silently strips all inline comments, which is a fatal developer-experience flaw.
`libcst` is a Concrete Syntax Tree library that preserves comments, whitespace, and formatting
intent. The ingestion layer uses `ast` for fast analysis (parse + walk) and `libcst` only at the
reconstruction boundary. `ruff format` normalizes style after reconstruction.

---

## 5. Conflict Detector

The conflict detector runs between step 2 and step 4 of the collection phase.
It catches semantic problems that AST validity alone cannot detect.

### 5.1 Checks

**Signature Compatibility** — If Agent A modified `func_b` and Agent B's fragment
calls `func_b`, verify that the call sites in B's fragment are still compatible
with A's new signature. This is a lightweight static check: extract the new
signature from A's committed version, parse all call expressions in B's fragment,
check arity and keyword argument names.

**Import Consistency** — If an agent added an import to `__header__`, check that
no other agent's concurrent header edit creates a duplicate or conflicting import.

**Name Collision** — If an agent introduced a new symbol (new function or class),
check that no other agent introduced a symbol with the same qualified name in the
same file in the same round.

**Cycle-Free Dependency Graph** — After all new nodes are registered, verify the
inter-node call graph (built from AST call expressions) is still acyclic within
the module, if the original was acyclic.

### 5.2 What It Does Not Check

The conflict detector is intentionally shallow — it is not a type checker or a
semantic analyser. It gates on `ast.parse()` success and the structural checks
above. Full correctness is the responsibility of the test suite, which agents
run before submitting output.

---

## 6. Agent Interface (The Adapter Layer)

This is the key architectural decision for swappability. The kernel never calls
model APIs directly. It speaks to an `AgentAdapter`, which translates between
MAK's internal protocol and the specific backend's interface.

**Architecture decision (revised):** The primary adapter target is **direct API integration**
(Anthropic SDK, OpenAI SDK), not CLI subprocess wrapping. CLI wrapping (`claude` CLI,
`codex` CLI) is brittle — any upstream output-format change breaks the adapter silently.
Direct API calls return structured JSON natively, eliminating the parsing fragility.
CLI-based adapters remain supported as a secondary option for agent types that have no
stable API (e.g., `gh copilot`), but MAK's first-party adapters use APIs.

### 6.1 Internal Protocol

MAK communicates with agents via newline-delimited JSON. For API-based adapters, this
JSON is sent as the user message in an API call; for subprocess-based adapters, over stdin.

**Task Assignment** (kernel → agent stdin):
```json
{
  "protocol_version": "1.0",
  "task_id": "042",
  "task_description": "Implement the acquire() method on RWLock to support read and write modes with queuing.",
  "write_fragments": [
    {
      "node_id": "mak/lock_manager/rwlock.py::function::RWLock.acquire",
      "current_source": "def acquire(self, mode: str) -> None:\n    pass\n",
      "version": 1
    }
  ],
  "read_context": [
    {
      "node_id": "mak/lock_manager/rwlock.py::class::RWLock",
      "source": "class RWLock:\n    ...",
      "note": "read-only context"
    }
  ],
  "constraints": {
    "style": "snake_case, type annotations required, no global state",
    "test_command": "pytest tests/lock_manager/ -v",
    "timeout_s": 300
  }
}
```

**Task Result** (agent stdout → kernel):
```json
{
  "protocol_version": "1.0",
  "task_id": "042",
  "status": "complete",
  "write_fragments": [
    {
      "node_id": "mak/lock_manager/rwlock.py::function::RWLock.acquire",
      "new_source": "def acquire(self, mode: str) -> None:\n    ...\n",
      "version": 2
    }
  ],
  "tests_passed": true,
  "git_commit_hash": "a3f9c1d",
  "notes": "Used threading.Condition for queue management."
}
```

Status values: `complete`, `partial`, `blocked`, `failed`.

### 6.2 AgentAdapter Interface

```python
# mak/agent_runner/adapter.py

class AgentAdapter(ABC):
    agent_id: str
    agent_type: str   # "claude_code" | "codex" | "gemini" | "copilot" | ...

    @abstractmethod
    def spawn(self, working_dir: str) -> subprocess.Popen: ...

    @abstractmethod
    def format_task(self, task_bundle: TaskBundle) -> str:
        """Translate TaskBundle into whatever the CLI tool expects on stdin."""
        ...

    @abstractmethod
    def parse_result(self, raw_output: str) -> TaskResult:
        """Parse the CLI tool's stdout into a TaskResult."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Verify the CLI tool is installed and responsive."""
        ...
```

Each supported agent gets its own adapter implementation:

```
mak/agent_runner/adapters/
├── base_adapter.py              # AgentAdapter ABC
├── anthropic_api_adapter.py    # PRIMARY: direct Anthropic SDK (claude-sonnet-4-6)
├── openai_api_adapter.py        # direct OpenAI SDK (gpt-4o, o3, etc.)
├── claude_code_adapter.py       # SECONDARY: wraps `claude` CLI (fallback)
├── codex_adapter.py             # wraps `codex` CLI
└── copilot_adapter.py           # wraps `gh copilot` CLI
```

API-based adapters (`anthropic_api_adapter`, `openai_api_adapter`) are built first and
are the default. CLI-based adapters are secondary and built only after the API layer is proven.

### 6.3 Adapter Registry

Adapters are registered at startup. The scheduler selects an adapter per task
based on availability and (optionally) task-type affinity.

```python
# mak/agent_runner/registry.py

ADAPTER_REGISTRY: dict[str, type[AgentAdapter]] = {
    "anthropic_api": AnthropicApiAdapter,   # primary
    "openai_api":    OpenAiApiAdapter,       # primary
    "claude_code":   ClaudeCodeAdapter,      # secondary (CLI fallback)
    "codex":         CodexAdapter,           # secondary (CLI fallback)
    "copilot":       CopilotAdapter,         # secondary (CLI fallback)
}

def get_adapter(agent_type: str) -> AgentAdapter:
    if agent_type not in ADAPTER_REGISTRY:
        raise UnknownAgentTypeError(f"No adapter registered for '{agent_type}'")
    return ADAPTER_REGISTRY[agent_type]()
```

Adding a new agent = writing one adapter class and registering it. The kernel
needs no changes.

### 6.4 Subprocess Lifecycle

```
1. Scheduler decides: Task T → Agent type A
2. AgentRunner checks pool: is an idle subprocess of type A available?
   - Yes: reuse it (send new task on stdin)
   - No: spawn new subprocess via adapter.spawn()
3. Send task_bundle as JSON line to subprocess stdin
4. Wait for JSON result line on stdout (with timeout)
5. On timeout: send SIGTERM, mark task as failed, re-queue
6. On result: pass to kernel for validation
7. On success: return subprocess to idle pool
8. On failure: discard subprocess (possible broken state), spawn fresh on next use
```

Subprocess pool size is configurable per agent type. Default: 2 concurrent
subprocesses per agent type.

---

## 7. Git Integration

Git is not used for isolation or conflict resolution. It is an audit log,
written by agents after MAK validates their output.

### 7.1 Agent Commit Protocol

After an agent receives a `complete` validation from MAK:

```
1. Agent runs: git add <files it modified>
2. Agent runs: git commit -m "[MAK-<task_id>] <description>"
   Commit message body includes:
     Module: <module>
     Locks: <node_ids>
     Status: complete
3. Agent writes commit hash into its TaskResult JSON
4. MAK records commit hash in session.log
```

Agents never `git push`. MAK coordinates a push at session end after all
tasks are complete and the full test suite passes.

### 7.2 Commit Message Format

```
[MAK-042] implement RWLock.acquire with queuing

Module: mak/lock_manager
Nodes: mak/lock_manager/rwlock.py::RWLock.acquire
       mak/lock_manager/rwlock.py::RWLock._wait_queue
Status: complete
Agent: claude_code
Session: mak-session-20250503-001
```

### 7.3 No Branches, No Worktrees

All commits go directly to the working branch. Lock discipline prevents
conflicting concurrent writes. The commit history is a linear, task-ordered
record of all changes. This is intentional — the history reflects the actual
order of validated changes, not a topology of parallel development branches.

---

## 8. Planner

The planner is the only module that calls an LLM. It takes the user's task
description and produces a list of subtasks with dependency edges.

```python
# mak/planner/planner.py

@dataclass
class SubTask:
    task_id: str
    description: str
    target_nodes: list[NodeId]      # nodes this task will write
    context_nodes: list[NodeId]     # nodes this task needs to read
    depends_on: list[str]           # task_ids that must complete first

class Planner:
    def decompose(self, user_task: str, node_inventory: list[NodeId]) -> list[SubTask]: ...
```

The planner prompt includes the current node inventory (qualified names only,
not source) so the LLM can make informed decisions about which nodes to target.
The output is parsed as JSON and validated against the `SubTask` schema before
being accepted. If parsing fails, the planner retries up to 3 times, then
raises `PlannerFailedError`.

**Human-in-the-Loop DAG review (required):** Before the planner's output is dispatched
to the scheduler, MAK presents the generated SubTask list and dependency edges to the
user for review and optional edit. A bad plan (missed dependency, hallucinated edge)
causes agent collisions or unnecessary serialization — errors that are expensive to
recover from mid-session. The review step costs ~5 seconds of human time and eliminates
the single-point-of-failure risk of 1-shot LLM DAG generation.

```
Planner output (JSON) → [USER APPROVES / EDITS] → Scheduler
```

The review is interactive by default and can be bypassed with `--no-review` for automated
pipelines that supply a pre-validated plan template.

**Design note**: if the user's task maps cleanly to a fixed architecture
(e.g., "add a new adapter for X"), the planner can be bypassed with a
hardcoded task template. The goal is to minimize LLM calls in the runtime path.

---

## 9. Scheduler

The scheduler converts the planner's `SubTask` list into an execution DAG and
drives agent assignment.

### 9.1 DAG Construction

```
1. Create a node per SubTask
2. For each depends_on edge, add a directed edge in the DAG
3. Topological sort → execution order
4. Tasks with no unmet dependencies → "ready" queue
5. As tasks complete, mark edges as satisfied; move newly unblocked tasks to ready queue
```

### 9.2 Lock Pre-allocation

Before dispatching a task, the scheduler attempts to acquire all required locks
(both write and read) in a single atomic operation. If any lock is unavailable,
the task stays in the pending queue. This prevents partial lock acquisition,
which is the classic deadlock setup.

```python
# mak/scheduler/scheduler.py

class Scheduler:
    def tick(self) -> None:
        """Called in a loop. Dispatches ready tasks to available agents."""
        for task in self.ready_queue:
            locks_available = self.lock_manager.try_acquire_all(
                task.target_nodes, mode="write",
                task.context_nodes, mode="read",
                holder=task.task_id
            )
            if locks_available:
                self.ready_queue.remove(task)
                self.dispatch(task)

    def dispatch(self, task: SubTask) -> None:
        adapter = self.agent_runner.get_available_adapter()
        self.agent_runner.assign(adapter, task)

    def on_task_complete(self, task_id: str) -> None:
        self.lock_manager.release_all(task_id)
        self.dag.mark_complete(task_id)
        self.ready_queue.extend(self.dag.newly_unblocked())
```

---

## 10. Module Structure

```
mak/
├── __main__.py                  # entry point: python -m mak --task "..."
├── config.py                    # config loading from mak/config.yaml
├── session.py                   # session lifecycle: init, run, teardown
│
├── core/
│   ├── types.py                 # NodeId, TaskBundle, TaskResult, SubTask, LockEntry
│   ├── exceptions.py            # all domain-specific exceptions
│   └── logging.py               # structured append-only session log
│
├── planner/
│   └── planner.py               # LLM decomposition, SubTask schema, retry logic
│
├── node_store/
│   ├── store.py                 # NodeStore: get, put, commit, rollback, reconstruct
│   ├── ingestion.py             # file → AST → node fragments
│   └── reconstruction.py       # node fragments → file (libcst + ruff)
│
├── lock_manager/
│   ├── lock_table.py            # LockTable: in-memory + persisted state
│   ├── rwlock.py                # per-node reader-writer lock primitive
│   └── deadlock_detector.py    # wait graph, cycle detection, resolution
│
├── scheduler/
│   ├── scheduler.py             # DAG traversal, task dispatch loop
│   └── dag.py                  # DAG construction, topological sort, edge tracking
│
├── conflict_detector/
│   ├── detector.py              # orchestrates all checks
│   ├── signature_check.py      # call site vs new signature compatibility
│   ├── import_check.py         # duplicate/conflicting import detection
│   └── name_collision_check.py # new symbol name uniqueness
│
├── agent_runner/
│   ├── runner.py               # subprocess pool, lifecycle, dispatch
│   ├── registry.py             # adapter registry
│   ├── protocol.py             # TaskBundle/TaskResult JSON serialization
│   └── adapters/
│       ├── base_adapter.py              # AgentAdapter ABC
│       ├── anthropic_api_adapter.py    # PRIMARY: Anthropic SDK
│       ├── openai_api_adapter.py        # PRIMARY: OpenAI SDK
│       ├── claude_code_adapter.py       # SECONDARY: claude CLI
│       ├── codex_adapter.py             # SECONDARY: codex CLI
│       └── copilot_adapter.py           # SECONDARY: gh copilot CLI
│
└── git_integration/
    └── git.py                  # commit helpers, push coordination, log parsing

tests/
├── node_store/
├── lock_manager/
├── scheduler/
├── conflict_detector/
└── agent_runner/

.mak/                           # runtime state (gitignored)
├── node_store/
├── lock_table.json
├── task_graph.json
└── session.log
```

---

## 11. Data Flow: End to End

```
User: "Implement topological sort in the scheduler module."
│
▼
Planner (LLM call)
  → SubTask A: implement TopologicalSorter.sort  [write: dag.py::sort]
  → SubTask B: implement Scheduler.tick          [write: scheduler.py::tick]
                                                 [read:  dag.py::sort]  ← depends on A
│
▼
DAG Builder
  A ──▶ B   (B depends on A)
│
▼
Scheduler tick #1
  A is ready (no unmet deps) → acquire write lock on dag.py::sort
  B is pending (waits for A)
  → dispatch A to Agent 0 (claude_code subprocess)
│
▼
Agent Runner
  → write TaskBundle JSON to Agent 0 stdin
  → Agent 0 receives fragment for TopologicalSorter.sort
  → Agent 0 edits, runs pytest, commits, writes TaskResult JSON to stdout
│
▼
Collection Phase
  → ast.parse(new fragment)           ✓
  → lock scope validation             ✓
  → cross-node conflict checks        ✓
  → commit dag.py::sort v2 to node store
  → reconstruct dag.py on disk
  → release lock on dag.py::sort
│
▼
Scheduler tick #2
  A is complete → B is now unblocked
  → acquire write lock on scheduler.py::tick + read lock on dag.py::sort
  → dispatch B to Agent 1 (or same Agent 0 if idle)
│
▼
[same collection phase for B]
│
▼
Session complete
  → run full test suite
  → if green: git push
  → write session summary to session.log
```

---

## 12. Configuration

`mak/config.yaml`:

```yaml
session:
  working_dir: "."
  max_concurrent_agents: 4
  lock_timeout_s: 300
  deadlock_check_interval_s: 5

planner:
  model: "claude-sonnet-4-20250514"
  max_retries: 3

agents:
  default: "anthropic_api"
  pool_size_per_type: 2
  available:
    - type: "anthropic_api"
      model: "claude-sonnet-4-6"
    - type: "openai_api"
      model: "gpt-4o"
    - type: "claude_code"     # CLI fallback
      cmd: "claude"

git:
  auto_commit: true
  auto_push: false
  branch: "main"

node_store:
  path: ".mak/node_store"
  parser: "libcst"            # libcst for comment-preserving reconstruction
  ruff_format: true

planner:
  hitl_review: true           # human reviews DAG before dispatch (use --no-review to skip)
```

---

## 13. Roadmap

### Phase 1 — Core Infrastructure ✅ COMPLETE (with one required correction)
- [x] `mak/core/types.py` — all shared types and schemas (`ResourceRef`, `NodeId`, `NodeFragment`, `LockEntry`, `TaskBundle`, `TaskResult`, `SubTask`, `LockMode` with `intent_write`)
- [x] `mak/core/exceptions.py` — all domain-specific exceptions (`NodeStoreError`, `PlannerFailedError`, `AgentError`, `UnknownAgentTypeError`, `ConfigError`)
- [x] `mak/core/logging.py` — structured append-only JSON Lines session logger (`EventType`, `LogEntry`, `SessionLogger`)
- [x] `mak/config.py` — config loading from `mak/config.yaml` (`MakConfig` dataclass tree with `SessionConfig`, `PlannerConfig`, `AgentConfig`, `GitConfig`, `NodeStoreConfig`)
- [x] `mak/node_store/` — ingestion (`parse_file_into_fragments`, `walk_and_parse`), versioned store (`NodeStore`: get/put/commit/rollback/list/reconstruct), reconstruction (`assemble_fragments`, `format_with_ruff`, `reconstruct_file`)
- [x] `mak/lock_manager/` — per-node RW lock (`RWLock`: read/write/intent_write modes, holder escalation), lock table (`LockTable`: atomic multi-lock, persistence, timeout expiry), deadlock detector (`DeadlockDetector`: wait graph, DFS cycle detection, wound-wait resolution)
- [x] `mak/agent_runner/` base — protocol serialization (`encode_task_bundle`/`decode_task_bundle`, `encode_task_result`/`decode_task_result`), `AgentAdapter` ABC, adapter registry (`ADAPTER_REGISTRY`, `register_adapter`, `get_adapter`, `list_adapters`)
- [ ] **[CORRECTION — PRE-CONDITION FOR WAVE 2]** Migrate `mak/node_store/reconstruction.py` from `ast.unparse()` to `libcst`: replace `normalize_with_ast()` with `libcst.parse_module()` + `.code`, add `libcst` to dependencies, update tests for comment-preservation. This must pass before any Wave 2 work begins.

### Phase 2 — Scheduling & Execution
- [ ] `mak/scheduler/dag.py` — DAG construction from `SubTask.depends_on` edges, topological sort, `mark_complete`, `newly_unblocked`, cycle validation
- [ ] `mak/scheduler/scheduler.py` — `Scheduler.tick()` loop: try_acquire_all → dispatch → on_task_complete, persist DAG state to `.mak/task_graph.json`
- [ ] `mak/agent_runner/runner.py` — agent runner: API-based dispatch (primary) + subprocess pool for CLI adapters (secondary), `assign(adapter, task)`, timeout handling, idle pool management
- [ ] `mak/agent_runner/adapters/anthropic_api_adapter.py` — **PRIMARY first adapter**: Anthropic SDK (`claude-sonnet-4-6`), formats TaskBundle as user message, returns TaskResult from response JSON. Structured output guarantee via `tool_use` or `json_object` response format.
- [ ] `mak/agent_runner/adapters/openai_api_adapter.py` — OpenAI SDK adapter using JSON mode

### Phase 3 — Validation & Safety
- [ ] `mak/conflict_detector/signature_check.py` — extract new signatures, compare call sites (arity + kwargs)
- [ ] `mak/conflict_detector/import_check.py` — detect duplicate/conflicting imports across concurrent `__header__` edits
- [ ] `mak/conflict_detector/name_collision_check.py` — detect new symbol name collisions within a file/round
- [ ] `mak/conflict_detector/detector.py` — `ConflictDetector` orchestrating all checks, returns pass/fail with reasons
- [ ] Crash recovery from `.mak/lock_table.json` (expired lock release + task re-queue)
- [ ] **Partial completion tracking**: accept committed sub-fragments on `status: partial`, re-queue only uncompleted node grants rather than the full task. Requires `SubTaskProgress` state in scheduler.

### Phase 4 — Planner, Git & Session
- [ ] `mak/planner/planner.py` — `Planner.decompose()`: LLM prompt with node inventory, JSON parse + SubTask validation, up to 3 retries, `PlannerFailedError` on exhaustion
- [ ] **HitL DAG review**: interactive terminal display of generated SubTask DAG; user can approve, edit edges, or abort before dispatch. Bypassable with `--no-review`.
- [ ] `mak/git_integration/git.py` — `GitHelper`: `commit_task`, `get_session_commits`, `push`, `validate_clean_state`
- [ ] `mak/session.py` — `Session` class: init (ingest codebase), run (planner → HitL review → scheduler → agent dispatch → conflict detection → commit), teardown (test suite, push)

### Phase 5 — CLI, Security & Evaluation
- [ ] `mak/__main__.py` — CLI entry point: `python -m mak --task "..."`, arg parsing (`--no-review`, `--agent`, `--config`), `Session` init + run, user-friendly errors
- [ ] **Agent sandboxing**: Docker-based subprocess isolation for CLI-type agents. Alpha/internal dev: unsandboxed acceptable. Required before public beta.
- [ ] `claude_code_adapter.py`, `codex_adapter.py`, `copilot_adapter.py` — secondary CLI adapters
- [ ] **`LanguageBackend` interface**: abstract ingestion + reconstruction behind a `LanguageBackend` ABC so `tree-sitter` backends can be added for TypeScript/Go without changing the kernel
- [ ] End-to-end integration tests: ingest small Python project, run MAK with mock agent, verify reconstructed output (comments preserved)
- [ ] Evaluation suite: run MAK on real Python projects, measure correctness and throughput vs. worktree baseline
- [ ] CLI polish, config validation, error messages

---

## 14. Resolved Decisions & Remaining Open Questions

**[RESOLVED] AST reconstruction: `libcst` over `ast.unparse()`** — `ast.unparse()` strips
all inline comments, which is a fatal developer-experience regression. Decision: `libcst`
is the production reconstruction backend. `ast` is retained for fast analysis (parse + walk)
only. Migration of `reconstruction.py` is a pre-condition for Wave 2.

**[RESOLVED] Agent adapter strategy: API-first, CLI-secondary** — CLI stdout scraping is
brittle against upstream formatting changes. Decision: `anthropic_api_adapter` and
`openai_api_adapter` are the primary first-party adapters. They use SDK clients to send
TaskBundle as a structured user message and parse TaskResult from the response. CLI adapters
remain available for tools with no stable API.

**[RESOLVED] Planner DAG validation: Human-in-the-Loop** — 1-shot LLM DAG generation is
unreliable for complex task graphs. Decision: add an interactive review step between Planner
output and Scheduler dispatch. The user sees the SubTask list and dependency edges and can
approve, edit, or abort. Bypassable with `--no-review` for automated pipelines.

**[PLANNED] Agent sandboxing** — External agent processes are an attack surface. For internal
dev and alpha: unsandboxed is acceptable. Required before public beta: Docker-based process
isolation with filesystem restrictions scoped to the working directory.

**[PLANNED] Partial completion** — `status: partial` currently re-queues the full task.
Target: accept committed sub-fragments, re-queue only uncompleted node grants. Tracked in
Phase 3 as `SubTaskProgress`.

**[OPEN] Multi-language support** — The AST pipeline is Python-specific. The node store schema
is language-agnostic; only ingestion and reconstruction are language-specific. Plan: abstract
behind a `LanguageBackend` interface (Phase 5) with `tree-sitter` as the target backend for
TypeScript/Go. Python proves the model; language-agnosticism follows.

**[OPEN] Planner quality at scale** — Even with HitL review, the planner's subtask
decomposition quality degrades on large tasks with many interdependent nodes. Improving prompt
engineering, few-shot examples, and possibly a multi-step planner (outline → detail) is an
ongoing research track.

---

## 15. Risk Register & Hardening Plan

> Source: `RISK_ASSESSMENT.md` (commit `47a4da6`). Every item below was reproduced against the
> code, not inferred from docs. IDs are referenced by **TASKS.md Wave H**.
>
> **STATUS: Wave H complete — all risks C1–C5, H1–H4, M1–M7, L1–L7 resolved.** 199 tests pass,
> `mypy --strict mak` clean, `ruff check mak tests` clean, CI added. The round-trip property test
> (§15.2) and concurrency stress test (§15.3) are green. The §15.1 table below is retained as the
> record of what was fixed.

### 15.1 Severity Ladder

| ID | Sev | One-line | Module | Remediation |
|----|-----|----------|--------|-------------|
| C1 | 🔴 | Decorators stripped on ingestion (`@property`, `@staticmethod`, `@app.route` lost) | `node_store/ingestion.py` | Span from `min(decorator lineno)`→`end_lineno`, not `get_source_segment` |
| C2 | 🔴 | Top-level statements reordered (alphabetical within kind buckets) on reconstruct | `node_store/reconstruction.py` | Store original `lineno` as metadata; sort by it; drop kind-bucket sort |
| C3 | 🔴 | Comments lost at **ingestion** — libcst-at-reconstruction can't recover them | `node_store/ingestion.py` | CST-based ingest so comments travel with each node |
| C4 | 🔴 | Method-level locking unimplemented — class is one atomic node | `node_store/ingestion.py` | Recurse into `ClassDef`; emit `file::function::Class.method` nodes |
| C5 | 🔴 | Duplicate node-id collision (overloads, conditional defs) silently drops symbols | `node_store/ingestion.py` | Disambiguate ids by position; raise `NodeStoreError` on collision |
| H1 | 🟠 | No thread safety; "atomic" acquire holds only under an unstated single-thread rule | `lock_manager/`, `node_store/` | Declare + enforce concurrency model; lock mutations or assert no-`await` spans |
| H2 | 🟠 | `intent_write` is a no-op; lock and deadlock detector use different conflict matrices | `lock_manager/rwlock.py`, `deadlock_detector.py` | One canonical conflict matrix consumed by both |
| H3 | 🟠 | Lease expiry silently steals a still-held lock (no heartbeat/notify) | `lock_manager/lock_table.py` | Renewal/heartbeat; on expiry fail+rollback the holder's task |
| H4 | 🟠 | Versioning unimplemented — no N+1, no rollback-to-prior, no version owner | `node_store/store.py` | Store owns `current+1` on `put_node`; keep prior versions |
| M1 | 🟡 | Implemented wire protocol ≠ documented (§6.1) | `agent_runner/protocol.py` | Reconcile to one schema, delete the other |
| M2 | 🟡 | `decode_task_bundle` leaves `locks` as raw dicts (type lie) | `agent_runner/protocol.py` | Reconstruct `LockEntry`/`ResourceRef` or drop from wire type |
| M3 | 🟡 | `AgentAdapter` ABC is subprocess-only but strategy is API-first | `agent_runner/adapters/base_adapter.py` | Split base ABC from `SubprocessAgentAdapter` |
| M4 | 🟡 | `mypy --strict` not clean (2 errors); README claims it is | `node_store/ingestion.py` | Guard `lineno` access; re-green strict |
| M5 | 🟡 | `ruff check` not clean (139 errors, 13 in `mak/`) | repo-wide | `--fix` + hand-fix; gate in CI |
| M6 | 🟡 | Config drift + unsafe coercion (`bool("false")`, bare `ValueError`) | `config.py` | Parse all documented keys; wrap in `ConfigError`; explicit bools |
| M7 | 🟡 | NodeStore API diverges from §2.3 (no version params; reconstruct off-store) | `node_store/store.py` | Align signatures/design; reach reconstruct from store |
| L1 | ⚪ | No CI / pre-commit on an agent-built codebase | repo | Actions: pytest + mypy --strict + ruff; pre-commit |
| L2 | ⚪ | Global mutable `ADAPTER_REGISTRY` (violates AGENTS.md) | `agent_runner/registry.py` | Injectable `AdapterRegistry` instance |
| L3 | ⚪ | `egg-info/` build artifact committed | repo | Remove + gitignore |
| L4 | ⚪ | PLANS/TASKS/AGENTS gitignored — design not version-controlled | `.gitignore` | Track design docs |
| L5 | ⚪ | `SessionLogger` not concurrency-safe (per-write open, no fsync/lock) | `core/logging.py` | Serialize writes; flush |
| L6 | ⚪ | Dead `normalize_with_ast`; `format_with_ruff` swallows failures silently | `node_store/reconstruction.py` | Delete dead code; log on ruff failure |
| L7 | ⚪ | `find_cycles` dupes cycles + unbounded recursion; `resolve` unwired | `lock_manager/deadlock_detector.py` | Canonical dedupe; iterative DFS; wire on scheduler land |

### 15.2 The Round-Trip Invariant (load-bearing)

The node store's contract must be: **`ingest(file) → store → reconstruct` yields a file that is
semantically equivalent to the original, with decorators, ordering, and comments intact.** This is
the property test that would have caught C1–C3, and it is the gate that makes "shared-memory editing"
trustworthy. It must exist and pass before any module that *writes* through the store (scheduler,
conflict detector, session) is built.

```
assert semantically_equivalent(original, reconstruct(ingest(original)))
  # for a corpus that includes: decorated defs, methods, module-level constants between
  # classes, top-level executable blocks, inline + standalone comments, @overload stubs.
```

### 15.3 Concurrency Model Decision (must be made explicit)

MAK is a concurrency arbiter, yet the concurrency model is undeclared. Resolve to one of:

- **(A) Single-threaded async kernel.** One event loop owns all lock/store mutation. Invariant: no
  `await` may span a check→acquire sequence. Cheaper; document and assert the invariant.
- **(B) Multi-threaded kernel.** Guard `LockTable`/`NodeStore`/`RWLock` mutations with a re-entrant
  lock. Required if agent dispatch ever runs on real threads.

Either way: add a stress test that drives concurrent acquire/release and asserts no two conflicting
holders ever coexist. Until this lands, "atomic" in §3.1 and §9.2 is aspirational, not guaranteed.

### 15.4 [RESOLVED] Ingestion uses raw-source span tiling, not `ast.unparse()` or `libcst`

The pre-Wave-2 "libcst migration" (§13, funding condition #1) assumed comments had to be recovered at
the reconstruction boundary. Per C3, comments were actually lost at *ingestion*. The resolution went
further than the libcst reframe: **ingestion now tiles a file into fragments that retain their raw
source text, and reconstruction concatenates those fragments in original order.** Nothing is ever
re-rendered through `ast.unparse()` or a CST, so comments, decorators, blank lines, and formatting
survive a round trip by construction.

Mechanics (`mak/node_store/ingestion.py`):
- The file is partitioned into line spans that tile it completely, in source order. Each top-level
  `def`/`class` is a fragment (decorator lines included); leading import/constant blocks become a
  `module_header`; interspersed top-level code becomes `module_body` fragments.
- Classes decompose one level: a `class` shell fragment plus one `method` fragment per method
  (`file::method::Class.method`), giving method-level lock granularity (C4).
- Duplicate names (e.g. `@overload`) are disambiguated with a `#n` id suffix (C5).
- Reconstruction (`reconstruction.py`) joins fragments in order and runs `ruff format`; the
  `ast.parse()` guard remains. The round-trip property test (`tests/node_store/test_roundtrip.py`)
  asserts AST-level equivalence + comment preservation over a corpus, and that MAK round-trips its
  own source.

Consequence: `libcst` is **not** a dependency, and the §13 "libcst migration" precondition is
withdrawn. One known follow-up: a `class` shell fragment (`class X:` with methods removed) is not
independently parseable, which mildly contradicts §2.2's "fragments parse in isolation" aspiration —
acceptable today because reconstruction validates the *assembled* file, not individual shells.

### 15.5 Concurrency model (resolved): thread-safe via a table-wide lock

Per §15.3, option B was chosen. `LockTable` and `NodeStore` guard every public mutation with a
re-entrant lock, so `try_acquire_all` is genuinely atomic and the stress test in
`tests/lock_manager/test_concurrency.py` confirms no two conflicting holders ever coexist under
contention. `intent_write` now excludes writers via the single conflict matrix in
`mak/lock_manager/conflicts.py`, consumed by both `RWLock` and `DeadlockDetector`. Lease expiry is
observable (`expire_stale()` returns expired leases, fires an `on_expire` callback, and logs) and
holders keep leases alive with `renew`/`renew_all` — no more silent lock theft.
