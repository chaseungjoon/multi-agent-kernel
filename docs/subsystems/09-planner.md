# 9. Planner

> [Contributing guide](../../CONTRIBUTING.md) · ← [8. Endpoints](08-endpoints.md) · [10. Git integration](10-git-integration.md) →
>
> **Code:** `mak/planner/` · **Tests:** `tests/planner/`, `tests/session/test_planning.py`

`mak/planner/` decomposes a task into a validated `SubTask` DAG. The planner is
shown the node inventory — ids, shapes and callers, never source — within a
token budget, and the kernel, not the model, supplies the callers a signature
change breaks.

**`planner.py`** — `Planner.plan(user_task, node_inventory, *, view=None,
observer=None) -> PlanOutcome` calls an injected `PlannerLLM`
(`complete(prompt) -> str`) and validates the reply with `parse_plan`;
`decompose(...)` is `plan(...).plan`. `planner.strategy` decides how much of the
inventory a prompt shows:

| Strategy | Inventory sent | Calls |
|---|---|---|
| `auto` (config default) | `full` when the whole level-1 view fits `inventory_token_budget`, else `retrieval` | — |
| `full` | every file at level 1: id suffixes, shapes, incoming references | 1 (+ retries) |
| `retrieval` | the repository tree and seed files, then the expansions the model asks for | 1 + ≤ `max_expansions` |
| `oneshot` | every node id as a flat list; byte-identical to the pre-0.10 prompt | 1 (+ retries) |
| `outline` | a file-level outline, then one detail call per step over that step's files (`outline.py`); ids are namespaced `s<k>.<id>` | 1 + steps |

A `Planner` built directly defaults to `oneshot`; `PlannerConfig` defaults to
`auto`. Without a `view`, one is built from the ids alone (no shapes or
references).

**Prompt layout.** `full` and `retrieval` prompts are append-only *stable*
blocks plus a *volatile* tail, with the task placed after the inventory so the
instructions and inventory are a prefix shared by every task in a session:

| Block | Content | Changes when |
|---|---|---|
| S1 | instructions, agent roster, tree (or the full view) | store generation, config, roster |
| S2 | user task and seed files | per plan |
| S3… | each expansion or verification round's material | appended per round |
| V | round directive and retry note | every attempt |

A retry changes only V, and each round's stable blocks are a byte prefix of the
next round's. A backend implementing `CachingPlannerLLM.complete_parts(stable,
volatile)` receives the split; an LLM with only `complete` gets
`"".join(stable) + volatile`. `oneshot` is one stable block plus the retry note.

**Callers.** The `full` / `retrieval` instructions tell the model that MAK knows
the static call graph (the `← refs` column): it declares `changes_api` and a
`contract` for each signature it changes, MAK adds the caller updates it can see
(`planner.auto_caller_tasks`), and the model adds only what the graph cannot see
— calls through `self` or an instance, dynamic dispatch, code other tasks in the
plan create. With `auto_caller_tasks: false` the paragraph asks the model to add
every caller itself. `oneshot` keeps its original "cascade prevention"
paragraph.

**Target rules** (`plan_schema.py`; each raises `ValueError`, which is fed back
to the model on retry):

- **Containment** first — a target must resolve inside the work dir
  (`unsafe_node_id_reason`).
- **Python-only targets** (`is_python_target`) — MAK has no AST node for other
  files.
- **One whole file, one task** — a whole-file target is owned by exactly one task.
- **One granularity per file** — a file is targeted whole or by symbols, never
  both.
- **Declarations** (`_coerce_subtask`) — `api_targets` / `contract` /
  `registry_keys` must name the task's own targets, a contract must parse and name
  the right symbol, and `changes_api: false` cannot accompany a contract. Setting
  `api_targets` or `contract` with `changes_api` null means `true`.

**Retries** (`max_retries`) cover the LLM call too: transient provider failures
back off exponentially (1s, 2s, 4s, capped at 8s); a `PlannerFailedError` from
the backend (missing SDK, refusal) is re-raised immediately; a rejected plan is
re-asked at once; a truncated reply is re-asked for a **smaller** plan.
`planner.self_critique: true` adds one reflection call that returns
`{"verdict": "ok"}` or a corrected plan; a failed critique keeps the original.

The order in `Session.plan()` is plan (+ critique) → validate → caller proposals
→ review → `install_plan` (which re-validates).

**`inventory.py`** — `InventoryView`, built from the inventory, the node
sources and the dependency graph, renders the inventory at three resolutions.
**Level 0** is the tree: directories and files with node counts. **Level 1** is
file detail:

```
app/billing/service.py  (3 nodes · used by 1 other file)
  ::module_header::__header__
  ::function::invoice_total  def invoice_total(items: list[float]) -> Invoice  ← 2 refs · 2 files (app/api/routes.py, this file)
  ::function::refresh  async def refresh(client, *, timeout: float=...) -> None
```

A node's full id is its file path plus the suffix. The shape comes from
`api_digest.node_signature(source, kind)`: `def` / `class` lines with every
default elided to `=...` (a default can hold a secret or an internal URL),
decorator names without arguments, no bodies or docstrings, one line capped at
160 characters, private names included. The `← refs` column is the reverse
index; `this file` when every referrer is local. **Flat** is the `oneshot`
listing. Truncation is never silent: `render_tree(max_tokens)` opens directories
breadth-first while they fit and renders the rest `[collapsed — expand to see]`;
`render_file(path, max_tokens)` shows a large file's first nodes and one
`… k more nodes not shown (budget)` line. Renderings are deterministic (sorted,
no timestamps, no absolute paths), because providers cache the prefix built from
them. File entries (and their signature parses) are built on first use. Golden
renderings live in `tests/planner/golden/`.

**`retrieval.py`** — the `Retriever` protocol (`seed(task, view, budget_tokens)
-> SeedResult`) picks the files a `retrieval` plan starts out seeing in detail.
`LexicalRetriever` extracts terms from the task (backticked spans, dotted names
and paths kept whole and split; identifiers split on camelCase, snake_case and
digits; a short stop-list), scores each node (exact symbol or file path 10, a
whole path segment 4, each shared sub-token its IDF over all symbol names), sums
each file's best five, gives the top five files' 1-hop neighbours half their
score, and takes files by score (then path) while they fit. A task of stop words
seeds nothing.

**`expansion.py`** — the `retrieval` rounds. A reply is a plan or
`{"expand": ["pkg/a.py", "pkg/b/"], "why": "…"}` (`parse_reply`; both at once is
a `ValueError`). A file path returns its level-1 detail; a directory returns its
contents one level deeper; an already-shown, unknown (with up to three close
paths) or non-`.py` path is reported — none of these is an error. Rules
(`RetrievalRounds`, `run_retrieval`):

- **Rounds.** Every expansion or verification spends one of `max_expansions`.
  When they (or the budget) run out, the prompt ends "Do not ask to expand
  further. Return the plan now." and a further expand reply is rejected like a
  malformed one. At most `max_expansions + 1` rounds.
- **Budget.** `inventory_token_budget` bounds the inventory **cumulatively**
  across the tree (≤ 25%), the seeds (≤ 35%) and every expansion; one file takes
  at most 25%. Requests that do not fit are listed "not expanded (inventory budget
  reached)" and counted.
- **Verification.** A plan that targets a non-existent id in an existing file the
  model never saw at level 1 gets one `verify` round showing that file, while
  rounds remain. An exact existing id is accepted either way; a target in a new
  file is legitimate.

**`callers.py`** — `find_missing_callers(plan, graph, referrers)` lists the
graph-visible callers of every `function` / `method` / `class` target of a task
with `changes_api: true` that no task covers (a caller is covered when a task
targets it or its whole file). `caller_findings` turns them into findings
(below). `propose_caller_tasks(plan, graph, referrers, max_tasks=…)` adds **one
task per caller file** for function and method targets: it targets that file's
uncovered callers, depends on every changing task whose target they reference,
reads the changed targets as context, declares `changes_api: false`, and is
described from the changer's `contract`. Ids are `mak.callers.<n>` in file order
(suffixed on a clash). A task that would break whole-file ownership or one
granularity per file is refused (`caller_task_refused`); files beyond the cap get
one `caller_tasks_capped` finding. Proposing runs only on the planner's path
(`PlanPreparer.propose`), never in validation, so a task the reviewer drops is
not re-added at install. `drop_tasks(plan, ids)` removes proposed tasks and any
validation-added edges into them.

**`telemetry.py`** — `PlannerCall` records one call: `phase` (`plan` /
`outline` / `detail` / `critique` / `expand` / `verify`), `round`, `attempt`,
`strategy`, `prompt_chars` and `stable_chars`, the inventory shown and left out
(`inventory_files_total` / `_shown`, `inventory_nodes_shown`, `inventory_chars`,
`collapsed_dirs`, `symbols_truncated`, `seed_files`, `expanded_paths`), usage
(`input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_write_tokens`),
`outcome` (`plan` / `expand` / `rejected` / `call_failed` / `truncated`) and
`duration_ms` — sizes and counts only. Calls reach the `observer` as they finish,
so a plan ending in `PlannerFailedError` still reports every call it paid for.
`PlanningSummary` folds a plan's calls into `planner_*` metrics; `PlanOutcome`
carries the plan, the strategy used, `seen_files` (for `retrieval`) and the
calls.

**`plan_schema.py`** holds `parse_plan` and its helpers (re-exported from
`planner.py`); **`outline.py`** the outline pass's parsing and assembly;
**`findings.py`** `PlanFinding` (re-exported from `validation.py`).

**`response.py`** — `loads_json` strips a fence found anywhere, skips framing
prose, and parses with `raw_decode`. On failure, `repair_truncated` distinguishes
**truncated** from **malformed** by closing open delimiters; a truncation raises
`TruncatedResponseError` with `complete_elements`. The repaired text is never
used as a plan.

**`llm.py`** — `PlannerLLM` backends: Anthropic (streamed), OpenAI, Gemini, and
`OllamaPlannerLLM`; each also implements `complete_parts`. Each reports a
provider-signalled cut as `TruncatedResponseError` before parsing; an Anthropic
`refusal` raises `PlannerFailedError`. `resolve_max_tokens(model)` delegates to
`resolve_output_budget` with a 4096–32000 clamp and 16384 fallback.

- **Caching.** Anthropic sends the prompt as content blocks with
  `cache_control: {"type": "ephemeral"}` on the first and last stable block (two
  breakpoints; none with `prompt_cache=False`). OpenAI and Gemini get the
  concatenation and cache byte-identical prefixes themselves. Ollama gets the
  concatenation with `num_ctx` sized once per plan — from
  `prompt_budget_tokens` plus a 4,096-token overhead, rounded up to 8,192 and
  clamped to the model — so later rounds do not reload the model. A prompt that
  cannot fit is refused (never silently truncated), suggesting `strategy: auto`
  and a lower `inventory_token_budget`.
- **Usage.** `last_usage["input_tokens"]` is every prompt token the provider
  processed, cached or not; `cached_input_tokens` is the cached subset:

  | Backend | `input_tokens` | `cached_input_tokens` | `cache_write_tokens` |
  |---|---|---|---|
  | Anthropic | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` |
  | OpenAI | `prompt_tokens` | `prompt_tokens_details.cached_tokens` | — |
  | Gemini | `prompt_token_count` | `cached_content_token_count` | — |
  | Ollama | `prompt_eval_count` | — | — |

  `session.max_total_tokens` therefore never under-counts a cached prompt.
  `Planner.token_usage` totals every call, including a truncated or rejected
  reply the backend reported usage for.
- `build_planner_llm(model, *, backend, base_url, api_key, timeout,
  prompt_cache, prompt_budget_tokens)` picks the backend: explicit `backend`
  first, then `base_url` → OpenAI-compatible, then the model-id prefix.

**`depgraph.py`** — `build_dep_graph(sources)` produces a `DepGraph` of
`references` (node → nodes it calls or reads) and `definers` (symbol → defining
nodes). Resolution is shallow and conservative: same-file calls, and cross-file
references only through a parsed import table to a uniquely resolvable file.
Anything ambiguous yields no edge, so the graph — and `referrers(graph)`, its
reverse index (node → nodes that reference it) — is a **lower bound**.
`store_sources` / `dep_graph_from_store` read committed state; the session
caches one graph per store generation ([§11](11-session-lifecycle.md)). `resolve_module_file` is the strict
module resolver the wave-end checks share.

**`validation.py`** — `validate_plan(plan, graph, inventory, semantic=None)`
returns a corrected copy plus `PlanFinding`s. The policy is asymmetric:

| Finding | Action |
|---|---|
| `missing_dep` | add the edge if acyclic-safe (mutual pairs are reported, not applied); also added when a task's context names a node another task creates |
| `corrected_node` | fix a hallucinated id only on one confident match (wrong kind segment, case/underscore variant, missing `Class.` prefix, or `difflib` ratio ≥ 0.9) |
| `unknown_node` | weak or multiple candidates — suggest, do not change |
| `unseen_target` | a `retrieval` plan's new id in an existing file the planner never saw (`PlanSemantics.seen_files`) — kept, flagged with same-file suggestions |
| `spurious_dep` | flag a declared edge with no reference either way; never remove |
| `context_dropped` | drop an unknown context node — unless another task in the plan targets it |
| `missing_caller` | a graph-visible caller of a declared API change that no task updates; reworded `added: …` when a proposed task covers it |
| `undeclared_api_callers` | one per task that changes function or method targets with outside callers without declaring `changes_api` |
| `caller_task_refused`, `caller_tasks_capped` | from caller proposals (above) |
| `relaxed_dep`, `declared_api_dep`, `shared_structure`, `ordered_table`, `registry_key_collision` | the declaration-driven rules from [§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic) |

The caller and unseen-target rules run only with `semantic` supplied. A
correction that would violate a `parse_plan` invariant is downgraded to a
suggestion.

**`contracts.py`** — `parse_contract`, `contract_stub`, and
`implementation_mismatch` ([§5.3](05-conflict-detection.md#53-semantic-conflicts-maksemantic)).

**`review.py`** — `display_plan_for_review` renders subtasks, edges, findings
(`is_applied`: ✎ for a change made, ⚠ for advice), and repair obligations, and
loops **approve / edit (paste JSON) / abort** (`PlanReviewAborted`). With
MAK-proposed tasks (marked `[proposed by MAK]`) it also offers **[d]rop
MAK-proposed tasks**, which removes them, re-renders and asks again. I/O is
injected. An optional `header` labels cascade waves. `--no-review` skips it.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [8. Endpoints](08-endpoints.md) · [10. Git integration](10-git-integration.md) →
