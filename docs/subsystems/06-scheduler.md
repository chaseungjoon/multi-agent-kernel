# 6. Scheduler

> [Contributing guide](../../CONTRIBUTING.md) · ← [5. Conflict detection](05-conflict-detection.md) · [7. Agent runner and adapters](07-agent-runner.md) →
>
> **Code:** `mak/scheduler/` · **Tests:** `tests/scheduler/`

`mak/scheduler/` turns a plan into running work.

- **`dag.py`** — `DAG` validates at construction: unique ids, known dependencies,
  no self-edges, acyclic (Kahn) — `SchedulingError` otherwise. Exposes a
  deterministic `topological_order()`, `mark_complete()`, and `newly_unblocked()`
  (each task handed out once). Soft dependencies ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)) are satisfied for dispatch
  but still respected by ordering.
- **`scheduler.py`** — `Scheduler.tick()` drains the ready queue under **atomic
  lock pre-allocation**: all of a task's lock requests (from `lock_requests`,
  [§4.5](04-lock-manager.md#45-derived-lock-resources-and-the-lock-policy)) are acquired in one `try_acquire_all`, or the task stays ready for the
  next tick. Partial acquisition never happens. `on_task_complete` releases locks
  and extends the ready queue; `on_task_failed` optionally re-queues. State
  persists to `.mak/task_graph.json` after every transition (atomically);
  `from_persisted(...)` rebuilds it for recovery, including `context_nodes`, read
  sets, lock policy, and annotations. An unparseable graph raises
  `SchedulingError`. Collaborators are injected behind `Protocol`s.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [5. Conflict detection](05-conflict-detection.md) · [7. Agent runner and adapters](07-agent-runner.md) →
