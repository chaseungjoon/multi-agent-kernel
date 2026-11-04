# Design principles

> [Contributing guide](../CONTRIBUTING.md) · ← [Overview](overview.md) · [Glossary](glossary.md) →

These are the rules the code already follows. When a change seems to need an
exception to one of them, raise it in review before you write the code. Each
rule links to the subsystem where it does the most work.

## Architecture

- **Shared memory over message passing.** Resolve conflicts at scheduling time,
  while the dependency graph is explicit ([Overview](overview.md#why-not-git-worktrees)).
- **The node store is the source of truth; files are derived.** This enables
  symbol-level locking and position-independent identity
  ([§2](subsystems/02-node-store.md)).
- **LLMs only where language understanding is needed.** Planning and fragment
  rewriting. Everything else is deterministic ([§9](subsystems/09-planner.md)).
- **Raw-source span tiling.** Comments and formatting survive by construction,
  with no CST dependency ([§3.1](subsystems/03-ast-pipeline.md#31-ingestion-ingestionpy)).
- **API-first adapters with forced structured output.** The agent stays a
  constrained fragment transform, not an autonomous editor
  ([§7.2](subsystems/07-agent-runner.md#72-api-adapters-primary)).
- **Human-in-the-loop plan review.** A few seconds of review remove the single
  point of failure in one-shot DAG generation
  ([§9](subsystems/09-planner.md)).

## Correctness

- **Atomic lock pre-allocation.** A waiting task holds nothing, so the pipeline is
  deadlock-free by construction ([§4.3](subsystems/04-lock-manager.md#43-deadlock-detection)).
- **One transaction per commit, with one commit point.** Validate before
  advancing the store, journal before touching disk, and recover by comparison,
  not by hope ([§11](subsystems/11-session-lifecycle.md#run)).
- **`compile()`, not `ast.parse()`, at every gate.** Reject what Python would
  reject ([§3.4](subsystems/03-ast-pipeline.md#34-collection-agent-output--node-store)).
- **Precision over recall in conflict detection.** A false conflict costs more
  than a missed one ([§5](subsystems/05-conflict-detection.md)).
- **Positive assertion, never absence of evidence.** When a positive and a
  negative outcome can look identical (a no-op vs. a truncated reply), require
  the positive case to assert itself, and accept assertions only about things
  that existed to be assessed ([§7.3](subsystems/07-agent-runner.md#73-output-budget-and-stop-signals)).
- **Enforce declarations; never trust them.** Contracts, body-only claims, and
  registry keys are checked at commit
  ([§5.3](subsystems/05-conflict-detection.md#53-semantic-conflicts-maksemantic)).
- **Bound what a run scans, not only what it sends.** A filter downstream of the
  work is not a bound ([§3.1](subsystems/03-ast-pipeline.md#31-ingestion-ingestionpy)).

## Observability and code

- **An event names what happened.** Outcomes get their own event types, not flags
  ([§1](subsystems/01-core.md)).
- **Evidence has provenance.** Catalog claims and runtime proof are kept apart;
  unknown is never collapsed into "unsupported"
  ([§8](subsystems/08-endpoints.md#structured-output-negotiation)).
- **No global mutable state.** Caches and registries are owned and injected
  ([§7.5](subsystems/07-agent-runner.md#75-registry-and-composition-root)).
- **Differential tests for "identical, only better" rewrites**
  ([Testing](testing.md)).

---

[Contributing guide](../CONTRIBUTING.md) · ← [Overview](overview.md) · [Glossary](glossary.md) →
