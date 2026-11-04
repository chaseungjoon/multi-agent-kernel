# 4. Lock manager

> [Contributing guide](../../CONTRIBUTING.md) · ← [3. The AST pipeline](03-ast-pipeline.md) · [5. Conflict detection](05-conflict-detection.md) →
>
> **Code:** `mak/lock_manager/`, `mak/scheduler/lock_policy.py`, `mak/semantic/locking.py` · **Tests:** `tests/lock_manager/`

`mak/lock_manager/` is the concurrency arbiter. The lock table is
**intra-process** by design; single ownership of a project across processes is
the project lease's job ([§4.4](#44-project-lease-project_leasepy)). MAK does not implement distributed locking.

## 4.1 Lock model

| Mode | Concurrent holders | Use |
|---|---|---|
| `read` | unlimited | read a resource as context |
| `write` | one, exclusive | edit a resource |
| `intent_write` | many; compatible with reads, excludes writers | hierarchy intention locks and keyed-registrar appends ([§4.5](#45-derived-lock-resources-and-the-lock-policy)) |

The conflict matrix lives once in `conflicts.py` and is used by both
`RWLock.can_acquire` and the `DeadlockDetector`, so they cannot disagree.

## 4.2 Lock table

`LockTable` (`lock_table.py`) holds state in memory and persists
`.mak/lock_table.json` atomically after every mutation. Methods: `try_acquire`,
`try_acquire_all` (atomic, all-or-nothing), `release`, `release_all`, `renew` /
`renew_all`, `expire_stale`, `clear`, and accessors. Every public mutation runs
under one table-wide re-entrant lock, so the check pass and acquire pass of
`try_acquire_all` cannot interleave. `tests/lock_manager/test_concurrency.py`
drives many threads at a shared node set and asserts no two conflicting holders
coexist.

Lease expiry is observable: an expiring lease is logged and reported via an
optional `on_expire` callback. A fresh session calls `clear()` (sound because it
holds the project lease); crash recovery keeps the persisted table and
`expire_stale`s it. An unreadable `lock_table.json` starts empty — every lease in
it is reconstructible.

## 4.3 Deadlock detection

`DeadlockDetector` builds a wait graph (A → B: A waits for a lock B holds),
finds cycles with an iterative deduplicated DFS, and resolves them wound-wait
style (abort the youngest, release its locks, re-queue it). Because the scheduler
pre-allocates all of a task's locks atomically, a waiting task holds none, so the
graph is acyclic by construction and the watchdog in `Session.run` is defense in
depth. The one state in which a task waits *while holding locks* is a parked
commit ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)), which the run loop resolves itself.

## 4.4 Project lease (`project_lease.py`)

`ProjectLease` guarantees **one owner per project**. A session takes it as the
first action of `initialize()` and `recover()`, renews it on the heartbeat, and
releases it in `close()`. A second live owner fails fast with `ProjectBusyError`
naming the holder's pid, host, session id, and heartbeat age. `mak gc` takes the
same lease.

On POSIX the lease is an `flock`, which the OS releases when the holder dies
however it dies — no timeout, no pid heuristic. The JSON record inside the file
is diagnostics. On Windows, `msvcrt` byte-range locks can outlive their process,
so that path falls back to a heartbeat-age threshold (`stale_after_s`, 90s); it is
the weaker path and is not exercised by CI (see Wave R in [`TASKS.md`](../../TASKS.md)).

## 4.5 Derived lock resources and the lock policy

One function builds every lock request — the scheduler's, the commit-time
re-validation's, and the deadlock watchdog's:
`mak/scheduler/lock_policy.py::lock_requests(task, policy)`. With every
`LockPolicy` flag off it returns WRITE on each target and READ on each
`context_node`, which makes the flags a clean ablation switch.
`mak/semantic/locking.py` builds the policy for a wave from config, store, and
code graph.

- **Interface/body split (`semantic.api_locks`).** `resources.py` derives two
  resources per node: `<id>#api` (its interface — signature, decorators, bases,
  fields, imports; whatever `api_digest.api_fingerprint` renders) and `<id>` (its
  body). A caller takes READ on `X#api`; a task that declared a body-only edit
  (`changes_api=False`) takes only `X` and runs beside X's callers. An undeclared
  task (`changes_api=None`) takes WRITE on every target's `#api`.
- **Intention locks (`semantic.intention_locks`).** A fragment write also takes
  INTENT_WRITE on its file id, and a method/`class_body` write on its class node.
  A whole-file or whole-class write needs WRITE at that level, which conflicts
  with any INTENT_WRITE below it — so a whole-file rewrite cannot start beside a
  fragment writer, while fragment writers of one file run together.
- **Key-level registry locks (`semantic.registry_keys`).** A *registrar* — a
  function whose body is a flat list of `callee("<literal>", …)` calls, detected
  structurally by `mak/node_store/registrar.py`, never by name — is commutative
  when every entry is keyed. An appender takes INTENT_WRITE on the node and WRITE
  on `<id>#key=<literal>` for each declared key (`SubTask.registry_keys`). An
  unkeyed list is order-dependent and keeps the plain node lock.

`Scheduler` accepts an optional `lock_policy` (`use_lock_policy` rebuilds it after
recovery), and `WaveState.granted` records the mode each task actually holds per
resource, so release and commit-time re-validation check the real mode.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [3. The AST pipeline](03-ast-pipeline.md) · [5. Conflict detection](05-conflict-detection.md) →
