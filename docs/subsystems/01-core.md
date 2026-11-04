# 1. Core types, exceptions, logging

> [Contributing guide](../../CONTRIBUTING.md) · [2. Node store](02-node-store.md) →
>
> **Code:** `mak/core/` · **Tests:** `tests/core/`

`mak/core/` holds the contracts every other module imports.

**`types.py`** — shared value objects, frozen dataclasses where possible:

- `NodeId` — `NewType(str)`; the identity of a lockable code unit (see
  [node identity](02-node-store.md#node-identity)).
- `NodeFragment` — a node's raw source plus `node_id`, `kind`, `version`.
- `LockMode` — `READ`, `WRITE`, `INTENT_WRITE` (`StrEnum`).
- `LockEntry` — one held lock (`node_id`, `mode`, `holder`, `task_id`,
  `acquired_at`, `timeout_s`).
- `ResourceRef` / `ResourceKind` — a file- or symbol-level resource reference.
- `TaskBundle` — sent *to* an agent: `task_id`, `description`, `target_nodes`,
  an enriched `context` dict, and `retry_note` (feedback for a re-dispatch, so a
  second attempt is never a byte-identical copy of the first).
- `TaskResult` — returned *from* an agent: `task_id`, `success`,
  `modified_nodes`, `new_sources`, `error`, plus `no_changes_required` (the
  agent's positive assertion that nothing needed changing), `stop_reason` and
  `usage` (from the provider), `retryable` (false for a failure that would repeat
  verbatim, such as a refusal), `error_kind` (`truncated` / `refused` /
  `protocol` / `api` / `context` / `stale_read`), and `repairs` (repair turns
  spent).
- `SubTask` — a planned unit of work: `task_id`, `description`, `target_nodes`
  (writes), `context_nodes` (reads), `depends_on`, `agent_type`, plus optional
  declarations `changes_api`, `api_targets`, `contract`, `registry_keys`, and
  kernel-owned `repair_obligations` (see [§11](11-session-lifecycle.md)).
- `RepairObligation` — a defect a fix-up task must resolve before it may commit.

**`task_codec.py`** — the one JSON shape for a `SubTask`, shared by the
planner's output and the persisted task graph, so a field can never exist in one
and be dropped by the other.

**`exceptions.py`** — every domain exception derives from `MakError`:
`LockError`, `SchedulingError`, `ConflictDetectionError`, `GitIntegrationError`,
`NodeStoreError`, `PlannerFailedError`, `PlanReviewAborted`, `SessionError`,
`AgentError`, `UnknownAgentTypeError`, `ConfigError`, `UnsafeNodeIdError`,
`ProjectBusyError`, `WorkTreeConflictError`, and `AgentResponseError` with its
subclasses:

| Exception | Meaning | Retryable |
|---|---|---|
| `AgentTruncatedError` | reply hit the output cap mid-generation | yes |
| `AgentRefusedError` | the model declined | no |
| `AgentProtocolError` | the HTTP call succeeded but the body did not decode into a `TaskResult` | yes |
| `AgentContextExceededError` | the bundle cannot fit the model's context window | no |

All carry `stop_reason` and `usage` so a failed attempt still accounts for its
tokens.

**`logging.py`** — `SessionLogger`, an append-only JSON-Lines event log.
`EventType` is a `StrEnum`; writes are serialized and flushed so events never
interleave. The rule behind the event set: **an event names what happened** — a
flag inside a payload is not a substitute for the right event type. Events that
exist so a run is diagnosable without re-running it:

- `TASK_DISPATCHED` — what the kernel *gave* the agent: context entry counts,
  bytes, a per-layer `layers` breakdown (`{count, bytes, nodes}`), and a
  `starved` flag.
- `AGENT_RESULT` — what came back, per attempt: granted and returned ids, source
  lengths, error, `no_changes_required`, `stop_reason`, `usage`.
- `SOURCE_DROPPED` — anything refused at staging, with the id and the grant.
- `ACCEPTED_NOOP`, `TASK_COMPLETED`, `TASK_FAILED`, `AGENT_REMAPPED`.
- `PLANNER_CALL` — one per planner LLM call, logged while planning runs: phase,
  round, attempt, strategy, prompt and cacheable-prefix size, how much of the
  inventory was shown or left out, token usage (cached input included) and
  outcome ([§9](09-planner.md)). Sizes and counts only, never prompt text; the first call of a plan
  also carries `index_build_ms`.
- `STALE_READ`, `API_ESCALATED`, `COMMIT_DEFERRED`, `GATE_FINDING`,
  `ADJUDICATION`, `CONFLICT_DETECTED`, `PLAN_VALIDATED`, `PLAN_METRICS`,
  `phase_span`.

**`atomic.py`** — `write_text_atomic`: temp file in the same directory, `fsync`,
`os.replace`. Every persisted state file goes through it, so a kill mid-write
leaves the whole old file or the whole new one.

**`paths.py`** — containment for the paths carried by node ids.
`unsafe_node_id_reason` / `check_node_id` reject an absolute path, a `..`
component, or anything under the MAK dir; `safe_path_under` additionally resolves
symlinks. A node id originates with a model and becomes a real filesystem path,
so containment is checked independently at the planner, at `install_plan`, and at
the node store.

**`budget.py`** — `resolve_output_budget(model, *, fallback, minimum, maximum)`,
the catalog lookup shared by the planner and the agent adapters.

---

[Contributing guide](../../CONTRIBUTING.md) · [2. Node store](02-node-store.md) →
