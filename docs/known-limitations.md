# Known limitations

> [Contributing guide](../CONTRIBUTING.md)

These are deliberate tradeoffs that fail safe. They are not bugs. Several are
already scheduled as waves in [`TASKS.md`](../TASKS.md). Before "fixing" one,
read the subsystem it links to, because the limitation is usually what keeps a
stronger guarantee intact.

- **Class shells are not independently parseable.** A task returning only a
  `class` shell is rejected by the parse gate, never written
  ([§3.6](subsystems/03-ast-pipeline.md#36-the-round-trip-invariant)).
- **Checks are shallow by design.** Calls through an untyped receiver are not
  checked, and module-level functions are keyed by bare name across a batch, so
  an unrelated same-named function in another file can cause a false positive (a
  bounded retry, not corruption). The wave-end checks skip metaclasses,
  unrecognised decorators, multiple resolved bases, and dynamic module bindings
  for the same reason ([§5](subsystems/05-conflict-detection.md)).
- **Behaviour changes behind an unchanged signature** need `impact_tests`, which
  is off by default because it runs the project's tests in subprocesses
  ([§5.3](subsystems/05-conflict-detection.md#detection-at-wave-end)).
- **Impacted-test selection is static** (import closure), so a test reaching a
  module only through late binding or dependency injection is missed.
- **Out-of-store files** (config, SQL, docs) are not nodes, so MAK neither locks
  nor checks them. Structured text files are planned in Wave 8.
- **The deadlock watchdog finds nothing by design.** Atomic pre-allocation makes
  the wait graph acyclic ([§4.3](subsystems/04-lock-manager.md#43-deadlock-detection)).
  Wave 32 turns it into a rare assertion.
- **Abandoning a wedged agent call is cooperative.** Python cannot kill a thread
  mid-call. The per-request timeout bounds it, and non-blocking shutdown stops
  the session from waiting on it ([§7.7](subsystems/07-agent-runner.md#77-the-runner-and-sandbox)).
  Wave 29 adds killable per-call processes.
- **One MAK per project.** Concurrent runs on one checkout are refused with
  `ProjectBusyError`. Distinct projects run fine.
- **The Windows project lease is weaker** than the POSIX `flock`
  ([§4.4](subsystems/04-lock-manager.md#44-project-lease-project_leasepy), and
  Wave R).
- **Reconciliation adopts the working tree by default**, so MAK builds on edits
  it never saw made. Use `on_external_edit: conflict` to stop instead
  ([§11](subsystems/11-session-lifecycle.md#initialize)).

---

[Contributing guide](../CONTRIBUTING.md)
