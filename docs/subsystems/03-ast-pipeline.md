# 3. The AST pipeline

> [Contributing guide](../../CONTRIBUTING.md) · ← [2. Node store](02-node-store.md) · [4. Lock manager](04-lock-manager.md) →
>
> **Code:** `mak/node_store/` (ingestion, `.makignore`, reconstruction, transaction), `mak/session/dispatch.py` · **Tests:** `tests/node_store/test_roundtrip.py`

The kernel's core mechanism — a structured replacement for diff/merge.

## 3.1 Ingestion (`ingestion.py`)

> Ingestion uses **raw-source span tiling**, not `ast.unparse()` and not
> `libcst`. The file is partitioned into line spans that tile it completely in
> source order, and each fragment keeps its raw text. Comments, decorators,
> blank lines, and formatting survive a round trip by construction.

- `ast.parse` for structure, then tile by line spans: the leading import/constant
  block → `module_header`; each top-level `def` → `function` (decorators
  included); top-level code between defs → `module_body`.
- Classes decompose **one level**: a `class` shell (class line, docstring, leading
  attributes), one `method` per method, and `class_body` fragments for statements
  after a method. This gives method-level lock granularity.
- `parse_file_into_fragments(path, source=None)` returns fragments in source
  order; `walk_and_parse(root, include, exclude)` runs it over a tree.
- `iter_source_files(root, include, exclude, skip=…, ignore=…)` walks the tree
  and **prunes excluded directories before descending**, so `.venv`,
  `node_modules`, and `site-packages` cost nothing. Include patterns use a small
  glob→regex translator whose `*` never crosses `/` and whose `**/` spans zero or
  more whole segments. A **trailing** `**` (`src/**`) matches every file below
  it at any depth — decided by MAK, not by the host Python, whose
  `Path.glob("src/**")` returns directories only before 3.13 and files too from
  3.13; it is the meaning `.makignore` gives `a/**`. Symlinked directories are
  not descended. A differential test pins every other shape against `Path.glob`;
  the trailing-`**` shape has an explicit expected list.

## 3.2 `.makignore` (`makignore.py`)

`node_store.exclude_patterns` is MAK's config-level list; `.makignore` is the
project's own gitignore-style list at the work-dir root, read on every
`Session.initialize()`. If missing, the first session writes one containing
`.mak/` and `.git/` (after the `git.require_clean_tree` check, so it cannot fail
that check on the run that creates it). An existing file is never overwritten.

| Pattern | Meaning |
|---|---|
| `# text` / blank | ignored; `\#` for a literal `#` |
| `name` | matches a file or directory named `name` at any depth |
| `dir/` | directories only (and everything under them) |
| `/top.py`, `pkg/mod.py` | a leading or middle `/` anchors to the work-dir root |
| `*`, `?`, `[a-z]` | wildcards that never cross `/` |
| `**/x`, `a/**/b`, `a/**` | `**` spans zero or more whole segments |
| `!pattern` | re-includes; `\!` for a literal `!` |

The last matching pattern wins, and — as in git — a file cannot be re-included
when a parent directory is ignored. `.makignore` applies to the walk
(`MakIgnore.matches`, pruning ignored directories) and to the startup prune
(`MakIgnore.is_ignored`, which checks every parent). It is not the safety net:
the session skips its own `mak_dir` unconditionally regardless of either list.

## 3.3 Dispatch: bundle enrichment

When a task is dispatched the session builds a `TaskBundle` and enriches it in
layers:

0. **Declared contracts** (`contract:<id>`) — the signatures the task's providers
   (and the task itself) declared, rendered with a role: "implement this" or
   "build against this fixed signature" ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)).
1. **Write targets** — committed source of every node the agent will write
   (`write_source:<id>`). A whole-file target whose file is stored as fragments
   gets the assembled file. Only a target that does not exist yet has no entry,
   and the agent is told that means "create it" (`NEW_TARGET_CONTRACT`).
2. **Planner context nodes** — read-only source (`read_source:<id>`).
3. **Same-file siblings** — every other committed node in the target files,
   skipped for a file that is itself a whole-file write target.
4. **Cross-file callers** — nodes elsewhere whose source references a target's
   symbol. A whole-file target contributes the symbols of its committed nodes.
5. **Dependency outputs** — the committed source of every target of every task
   this one **directly** `depends_on`.

**Layer 4's filters and budget.** Only names that could be node ids (functions,
classes, methods) count as symbols — never module-level bindings like `__all__`.
A symbol shorter than 4 characters is ignored, a symbol matching more than 8
nodes is discarded as uninformative, and survivors are ranked (most matches,
then smallest, then id) until `session.cross_file_context_bytes` (default
32000) is spent. Past the budget an entry is **dropped** and counted as
`cross_file_dropped` on the dispatch event. The lookup is an inverted
`symbol → [node_id]` index keyed on `NodeStore.generation`, equivalent to the
word-boundary regex it replaces (a differential test pins that).

**Layer 5's budget.** `session.dependency_context_bytes` (default 24000; `0`
disables, `-1` unbounded). Past it, an entry **degrades to a public API digest**
(`read_api:<id>` — signatures, class members, constants, from
`mak/node_store/api_digest.py`) instead of being dropped: a dependent needs its
dependency's contract more than its implementation.

**The starvation guard.** A bundle that ends enrichment with zero context entries
while its task declares `depends_on` or `context_nodes` is a kernel defect. It is
never sent; the task fails immediately with `retryable=False` naming the defect.

Every attempt logs a `TASK_DISPATCHED` event, and the task's **read set** is
captured on the dispatching thread right after enrichment ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)).

## 3.4 Collection (agent output → node store)

1. **Map returned ids onto the grant** (`protocol.map_returned_sources`). Granted
   ids pass through; a symbol id inside a whole-file grant is **folded** into that
   grant; anything else is refused and logged as `SOURCE_DROPPED`. Folded
   fragments are ordered: a `module_header` first, then the store's recorded
   `order`, then emission order.
   If **every** staged source equals its committed source, the reply carries no
   work: the fragments are rolled back, each logged as `SOURCE_DROPPED`
   ("identical to the committed source"), and the attempt is judged as an empty
   reply ([§11 Run](11-session-lifecycle.md#run), no-op acceptance). An unchanged
   node beside a changed one commits with it.
2. **`compile()` each fragment** — not `ast.parse()`, because `compile()`
   enforces every compile-time rule, including `from __future__` placement.
3. **The commit pipeline** ([§5](05-conflict-detection.md), [§11](11-session-lifecycle.md)).
4. **Transactional commit and reconstruction** ([§11](11-session-lifecycle.md)). On success, release the
   task's locks and write an audit commit; on failure the whole transaction rolls
   back.

## 3.5 Reconstruction (`reconstruction.py`)

`assemble_fragments` concatenates fragments in stored order. `reconstruct_file`
assembles, runs `compile()` as a guard, formats with `ruff format`, and writes.
`ruff` is a **runtime dependency**: `_find_ruff()` looks beside `sys.executable`,
then on `PATH`. If formatting fails, the raw source is written and the failure is
logged — formatting never fails a run.

`transaction.py` renders every affected file in memory, journals each
destination's prior content, then installs all of them with `write_text_atomic`,
so a multi-file change is all-or-nothing on disk.

## 3.6 The round-trip invariant

```
ingest(file) → store → reconstruct  ≡  the original, semantically,
with decorators, statement order, and comments intact.
```

`tests/node_store/test_roundtrip.py` is a property test over decorated defs,
methods, constants between classes, top-level blocks, comments, and `@overload`
stubs — plus MAK's own source. **If you touch ingestion or reconstruction, this
test is your gate.**

A class shell fragment is not independently parseable; that is acceptable because
reconstruction validates the assembled file. Agents must return method source
with its original indentation — a dedented method fails the assembled-file gate
and is rejected, never written.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [2. Node store](02-node-store.md) · [4. Lock manager](04-lock-manager.md) →
