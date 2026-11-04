# 2. Node store

> [Contributing guide](../../CONTRIBUTING.md) · ← [1. Core types, exceptions, logging](01-core.md) · [3. The AST pipeline](03-ast-pipeline.md) →
>
> **Code:** `mak/node_store/` · **Tests:** `tests/node_store/`

`mak/node_store/` is MAK's shared memory. It replaces the filesystem as the
source of truth for code.

## Node identity

A **node** is the smallest independently lockable unit of code. Identity is
**position-independent** — based on qualified name, not line number — so
inserting a new function does not invalidate another agent's lock.

```
<file_path>::<kind>::<qualified_name>
```

| Kind | Example id |
|---|---|
| `function` (top-level def) | `mak/scheduler/dag.py::function::topological_order` |
| `class` (the class *shell*) | `mak/lock_manager/rwlock.py::class::RWLock` |
| `method` | `mak/lock_manager/rwlock.py::method::RWLock.acquire` |
| `module_header` (imports + leading constants) | `mak/config.py::module_header::__header__` |
| `module_body` (top-level code after the first def/class) | `mak/config.py::module_body::__body__` |
| `class_body` (class-level statements after a method) | `…::class_body::RWLock` |

Duplicate names (`@overload` stubs, conditional defs) get a `#n` suffix so no
symbol is dropped.

**Whole-file nodes.** A node id may be a **bare file path** — `app/main.py`. The
agent returns the entire file as one node, which reconstruction writes verbatim.
This is how MAK creates a new file (`reconstruct_file` creates parent
directories) and how a task rewrites an existing module wholesale. Committing a
whole-file node **supersedes** the file's fragments: `commit_node` drops every
`path::…` node for that file, `list_nodes(file_path)` returns only the whole-file
node, `list_nodes()` omits the superseded fragments from the planner inventory,
and `parse_file_into_nodes` keeps a committed whole-file node whole (a differing
source becomes its next version). A whole-file node lives for one session only:
reconciliation at the next session start (`sync_file(..., refragment=True)`)
retires it and ingests the file as symbol fragments, so later plans can divide
the file across tasks. Two tasks that both target the same bare path are merged
into one by `parse_plan`; mixing whole-file and fragment targets for the same
file in one plan is rejected at plan time ([§9](09-planner.md)).

## On-disk layout

Runtime state lives under `.mak/` in the work dir:

```
.mak/
├── node_store/
│   ├── metadata.json         ← index: kind, order, version, retired flag per node
│   ├── file_state.json       ← SHA-256 of what MAK last materialized per file
│   └── <mirrored source tree>/<file>.py/
│       ├── __header__.v1.py
│       ├── <Class>.v1.py
│       └── <Class.method>.v1.py
├── journal/                  ← write-ahead commit journal
├── lock_table.json           ← persisted lock state
├── owner.lock                ← the project lease
├── task_graph.json           ← DAG execution state, for --recover
└── session.log               ← append-only event log
```

## `NodeStore` API

Key methods on `NodeStore` (`store.py`): `get_node`, `put_node`, `commit_node`,
`rollback_node`, `revert_node`, `uncommit_node`, `retire_node`, `remove_node`,
`get_staged`, `list_nodes`, `list_all_nodes`, `get_committed_fragments`,
`get_preview_fragments`, `parse_file_into_nodes`, `sync_file`, `transaction`,
`node_order`, `record_materialized` / `materialized_digest`, `gc`, and the
`generation` counter.

- **The store owns version assignment.** `put_node` stamps
  `current_committed + 1`; callers never guess. All mutations run under one
  re-entrant lock.
- **Five kinds of undo, each for one job:**

  | Method | Undoes | Keeps history? |
  |---|---|---|
  | `rollback_node` | a pending (staged) fragment | n/a |
  | `revert_node` | one committed version, to `version - 1` | yes |
  | `uncommit_node` | a first-version commit, back to absent | yes |
  | `retire_node` | a symbol the working tree no longer has | yes |
  | `remove_node` | everything, permanently — maintenance only | no |

  A retired node leaves the live set (no listing, no reconstruction, no planner
  inventory) but its metadata and versions stay, so `get_node(nid, version=n)`
  still answers and `gc` does not treat its directory as an orphan.
- **`transaction()` is the commit point.** Inside a transaction the destructive
  effects — superseded-fragment deletion, version pruning, and the metadata save
  — are deferred; `_save_metadata` runs once at the end, and **that save is the
  commit point**. Before it the in-memory index is restored verbatim on failure;
  after it the deferred deletions drain. Transactions nest by depth; only the
  outermost commits or rolls back.
- **`get_preview_fragments(file, staged_overrides)`** builds the prospective file
  before committing, substituting staged versions, re-applying each fragment's
  `indent_prefix`, and superseding fragments for a staged whole-file node exactly
  as the commit would.
- **Retention.** A commit prunes the node back to `node_store.version_retention`
  versions (default 5, floor 2 because `revert_node` needs one prior version,
  `-1` keeps everything). `gc()` applies retention store-wide and removes orphan
  directories by *forward*-mapping every live id to its directory — never by
  parsing a path back into an id, since `a/b.py` and `a/b.py::function::f` nest
  inside one another on disk.
- **Ordering and `generation`.** Listings sort by `(file_path, order)` and are
  memoized; the memo is invalidated whenever the committed set changes, and the
  same invalidation increments `generation`, which callers use to cache derived
  state (the session's symbol index, the wave-end check results).
- **Crash safety.** `metadata.json` is written atomically. An unreadable one is
  quarantined to `metadata.json.corrupt` and the store starts with an empty index;
  fragments on disk are left untouched. `file_state.json` that cannot be read
  counts as "never seen", so reconciliation trusts the working tree.
- **Containment.** `_fragment_dir`, the single choke point for fragment I/O,
  asserts `check_node_id`.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [1. Core types, exceptions, logging](01-core.md) · [3. The AST pipeline](03-ast-pipeline.md) →
