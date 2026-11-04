# Glossary

> [Contributing guide](../CONTRIBUTING.md) · ← [Design principles](design-principles.md)

The terms used across the docs and the code, in reading order rather than
alphabetical order. Each links to where it is defined in depth.

## Code and storage

- **Node**: the smallest independently lockable unit of code
  ([§2](subsystems/02-node-store.md#node-identity)).
- **NodeId**: `<file>::<kind>::<qualified_name>`, or a bare path for a whole-file
  node.
- **Fragment (`NodeFragment`)**: a node's raw source with version and order
  metadata.
- **Registrar**: a function whose body is a flat list of keyed registration
  calls ([§4.5](subsystems/04-lock-manager.md#45-derived-lock-resources-and-the-lock-policy)).
- **`.mak/`**: the project's MAK directory. It holds an optional `config.yaml`
  and the runtime state (node store, journal, lock table, lease, task graph,
  session log). The user-level config is `~/.config/mak/config.yaml`.
- **`.makignore`**: the project's gitignore-style list of paths MAK never
  ingests ([§3.2](subsystems/03-ast-pipeline.md#32-makignore-makignorepy)).

## Planning and execution

- **SubTask**: a planned unit of work with write targets, read context,
  dependencies, and optional declarations ([§1](subsystems/01-core.md)).
- **Planner strategy**: how much of the inventory the planner is shown: `auto`,
  `full`, `retrieval`, `oneshot` or `outline` ([§9](subsystems/09-planner.md)).
- **Inventory view**: the inventory rendered as a tree (level 0), as file detail
  with shapes and callers (level 1), or as a flat id list. An **expansion** is a
  `retrieval` round in which the planner asks to see more.
- **Caller task**: a `mak.callers.<n>` task MAK proposes to update the callers a
  declared signature change breaks. The reviewer may drop it.
- **Wave**: one run of a plan. Cascade waves are the fix-up plans that follow
  ([§11](subsystems/11-session-lifecycle.md#cascade-waves-and-repair-obligations)).
- **TaskBundle / TaskResult**: the wire objects sent to and returned from an
  agent ([§7.6](subsystems/07-agent-runner.md#76-the-wire-protocol-protocolpy)).

## Conflict detection

- **EditRound**: the staged fragments the structural checks validate together
  ([§5.1](subsystems/05-conflict-detection.md#51-structural-checks-makconflict_detector)).
- **Read set**: the digests of every node a task was shown, checked at commit.
- **Contract**: a declared signature, enforced at commit.
- **Repair obligation**: a defect a fix-up task must resolve before committing.

## Agents and configuration

- **Adapter**: the translator between MAK's protocol and one agent backend
  ([§7](subsystems/07-agent-runner.md)).
- **Endpoint**: a configured model service. Its **profile** is its preset
  defaults and its **transport** is its wire protocol
  ([§8](subsystems/08-endpoints.md)).
- **Composition root**: `mak/bootstrap.py`. The **application API** is
  `mak/application/`, which both front ends use to build a run
  ([§13](subsystems/13-command-line.md#the-application-api-makapplication)).
- **PlannerRoute**: the planner's route (hosted, endpoint or local) as one
  complete value.

---

[Contributing guide](../CONTRIBUTING.md) · ← [Design principles](design-principles.md)
