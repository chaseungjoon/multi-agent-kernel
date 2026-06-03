# RISK_ASSESSMENT.md — Multi Agent Kernel

> Intensive risk assessment and quality analysis of the MAK codebase as of commit `47a4da6`.
> Scope: all of `mak/` (1,482 LOC), tests, config, packaging, and process. Every finding below
> was reproduced or confirmed directly against the code, not inferred from documentation.
> Remediation is tracked in **PLANS.md §15 (Risk Register)** and **TASKS.md Wave H (Hardening)**.

---

## 0. Executive Summary

MAK is a clean, well-documented, well-tested *foundation* — 148 passing tests, frozen dataclasses,
domain exceptions, a coherent design narrative. But the project's **single headline guarantee —
"agents edit a shared codebase with no merge conflicts, and files are reconstructed faithfully with
comments preserved" — does not hold today.** The AST ingest/reconstruct pipeline silently corrupts
real-world Python, and the concurrency layer that the entire premise rests on is neither thread-safe
nor semantically complete.

The danger is not that the code crashes. It is that it **succeeds silently while producing wrong
output**: stripped decorators, reordered top-level code, dropped comments, and lock modes that grant
access they claim to deny. For a tool whose value proposition is "trust us to arbitrate concurrent
writes to your source," silent corruption is the worst possible failure mode.

| Severity | Count | Theme |
|---|---|---|
| 🔴 Critical | 5 | AST pipeline silently loses or reorders code (data corruption) |
| 🟠 High | 4 | Concurrency model unsafe / incomplete (core value prop) |
| 🟡 Medium | 7 | Contract drift, broken type/quality gates, config |
| ⚪ Low | 7 | Process, hygiene, global state, dead code |

**The good news:** every critical finding is local to `node_store/` and `lock_manager/` — the two
modules marked "Complete." None require an architecture change. They require correctness fixes plus
the round-trip property tests that should have caught them. **Recommendation: insert a hardening
wave (Wave H) before Wave 2. Do not build the scheduler, conflict detector, or API adapters on top
of a node store that cannot round-trip its own source files.**

> Self-test: MAK cannot currently process its own codebase without corruption. `mak/config.py`
> defines module-level constants between dataclasses; `reconstruction.py` uses `@staticmethod` /
> `@property`-style decorators in `rwlock.py`. Both would be mangled by an ingest→reconstruct cycle.

---

## 1. 🔴 Critical — AST Pipeline Corrupts Source

These were reproduced with a 20-line script (`parse_file_into_fragments` → `assemble_fragments`).
Input order: `zebra` (decorated), `Alpha` (class), `apple`; output shown per finding.

### C1 — Decorators are stripped on ingestion
`ast.get_source_segment(source, node)` for a `FunctionDef`/`ClassDef` starts at the `def`/`class`
line — **decorators live above that line and are excluded.** Confirmed: an `@staticmethod`-decorated
function ingests as `def zebra():` with the decorator gone.

> **Impact:** Every `@property`, `@staticmethod`, `@classmethod`, `@dataclass`, `@app.route`,
> `@pytest.fixture`, `@functools.cache` is destroyed on the first ingest. This silently changes
> program semantics (a property becomes a method; a route stops being registered). MAK ingests its
> own decorated code, so it cannot bootstrap on itself.
>
> **Fix:** Compute the fragment span from `min(d.lineno for d in node.decorator_list)` (when present)
> through `node.end_lineno`, not from `ast.get_source_segment`. Add a decorated-symbol round-trip test.

### C2 — Top-level statements are reordered on reconstruction
`assemble_fragments` sorts by `(_KIND_ORDER, node_id_string)` — i.e. all headers, then all classes,
then all functions, then body — **and within each bucket alphabetically by node id.** Original source
order is discarded (it is never stored as metadata). Confirmed: input `zebra, Alpha, apple` reconstructs
as `Alpha, apple, zebra` — `zebra` moved from first to last.

> **Impact:** Any module where definition order matters breaks: a function calling another defined
> below it at module scope, a class used by a module-level constant, decorators referencing
> later-defined names, top-level execution order. This produces **import-time `NameError`s and
> behavior changes that pass `ast.parse()`** (so the reconstruction guard does not catch them).
>
> **Fix:** Record each node's original `lineno` as fragment metadata at ingestion; sort by it on
> reconstruction. Drop the kind-based bucket sort entirely (it is the cause, not a safety net).

### C3 — Comments are lost at ingestion, before libcst ever runs
The README's flagship claim — *"comments are always preserved" via libcst — is false at the point
that matters. `ast`-based ingestion drops: (a) standalone comments between top-level nodes, (b)
trailing inline comments (`return CONST  # trailing` → `return CONST`), and (c) all module-level
comments not physically inside a node body. Confirmed in repro.

> **Impact:** Comments are gone the instant a file is ingested. The planned libcst migration only
> touches *reconstruction normalization* — it operates on fragments that **already lost their
> comments.** The pre-Wave-2 "funding condition #1" therefore does not deliver the benefit it claims.
>
> **Fix:** Move to a CST (libcst) at the *ingestion boundary*, not just reconstruction — split the
> module into nodes using libcst so leading/trailing comments and whitespace travel with each node.
> This reframes the libcst task from "swap unparse" to "CST-based ingest + reconstruct."

### C4 — Method-level locking is unimplemented; classes lock as one unit
`parse_file_into_fragments` only emits top-level `function`/`class` nodes. Methods inside a class are
**not** separate fragments — the whole class is one node. Yet PLANS §2.1 and §6.1 advertise node ids
like `RWLock.acquire` (method granularity), and the entire pitch is "symbol-level locks so two agents
can edit the same file."

> **Impact:** Two agents cannot edit two methods of the same class concurrently — they collide on the
> single class node. The headline concurrency granularity does not exist; effective granularity is
> "top-level symbol," which for class-heavy code means "whole class." This is a capability gap, not a
> crash, but it directly contradicts the design and README.
>
> **Fix:** Recurse into `ClassDef` bodies, emitting one node per method with a qualified id
> (`file::function::Class.method`). Decide and document how the class "shell" (decorators, class-level
> attrs, docstring) is stored and reassembled around its method nodes.

### C5 — Duplicate node-id collision silently drops symbols
Node id is `path::kind::name` with no disambiguator. Two top-level defs sharing a name — `@overload`
stubs, `if TYPE_CHECKING:` / else duplicate defs, redefinition — produce the **same id**. In
`walk_and_parse`/`NodeStore`, the later one overwrites the earlier; one definition is lost.

> **Impact:** Overload-heavy or conditionally-defined modules lose code on ingest with no error.
> **Fix:** Disambiguate ids by source position or occurrence index; detect collisions and raise
> `NodeStoreError` rather than overwrite.

---

## 2. 🟠 High — Concurrency Model Unsafe / Incomplete

The product *is* a concurrency arbiter. These findings undercut that directly.

### H1 — Nothing is thread-safe; "atomic" holds only under an unstated assumption
`RWLock`, `LockTable`, and `NodeStore` mutate plain `dict`/`set` with **no `threading.Lock`**.
`try_acquire_all` does check-all-then-acquire-all in two passes; this is atomic **only** if no other
thread (and no `await`) interleaves between the passes. That single-threaded-event-loop assumption is
nowhere stated or enforced, while README/PLANS describe agents running concurrently.

> **Impact:** The moment dispatch becomes genuinely concurrent (threads or interleaved coroutines),
> `try_acquire_all` has a TOCTOU race — two holders can both pass the check pass and both acquire
> conflicting locks. The corruption MAK exists to prevent becomes possible *inside the lock manager.*
>
> **Fix:** Decide the concurrency model explicitly. Either (a) single-threaded async kernel —
> document the invariant and assert no `await` spans a check/acquire, or (b) guard `LockTable`/
> `NodeStore`/`RWLock` mutations with a re-entrant lock. Add a multi-threaded stress test either way.

### H2 — `intent_write` is a no-op; two conflict matrices disagree
`RWLock.can_acquire(WRITE)` checks only readers and the writer — it **ignores `intent_writers`
entirely.** Confirmed: holder A takes `intent_write`, holder B's `can_acquire(WRITE)` returns `True`.
Meanwhile `DeadlockDetector._conflicts` treats `intent_write` vs `write` as conflicting. The lock and
the deadlock detector implement **different conflict models.**

> **Impact:** `intent_write` provides zero exclusion — it is dead weight that the deadlock detector
> nonetheless reasons about, so deadlock decisions can be made on edges the lock layer doesn't honor.
> **Fix:** Define one canonical conflict matrix in a single place; have both `RWLock` and
> `DeadlockDetector` consume it. Decide intent_write's real semantics (typically: blocks new writers,
> allows readers) and enforce it.

### H3 — Lock-lease expiry silently steals a still-held lock
`_expire_stale()` releases any lock older than `default_timeout` (300s) with **no heartbeat, renewal,
or holder notification.** A slow-but-alive agent past 300s loses its write lock; another agent can
then grab the same node and both write it. Expiry also only fires opportunistically inside
`try_acquire*` — never on a timer — so it's both unsafe *and* unreliable.

> **Impact:** Long-running tasks → silent concurrent writes to the same symbol → the exact corruption
> the kernel promises to prevent. **Fix:** Add lease renewal/heartbeat from the agent runner; on
> expiry, treat the holder's task as failed and roll back its pending node versions before reassigning.

### H4 — Versioning is not implemented
`parse_file_into_nodes` always stamps `version=1`; `put_node`/`commit_node` store whatever version the
caller passes; nothing increments. PLANS §4.3's "write new fragment as version N+1, then commit/rollback"
optimistic-concurrency model is absent, and it is undefined who assigns versions to agent output.

> **Impact:** No rollback-to-previous-version, no optimistic conflict detection, no audit trail of
> symbol evolution — all of which downstream waves assume exist. **Fix:** Make the store own version
> assignment (`current + 1` on `put_node`), keep prior versions, and define commit/rollback against them.

---

## 3. 🟡 Medium — Contract Drift, Broken Gates, Config

### M1 — Implemented wire protocol ≠ documented wire protocol
PLANS §6.1 specifies `write_fragments`, `read_context`, `status`, `tests_passed`, `git_commit_hash`.
The actual `TaskBundle`/`TaskResult` use `description`, `target_nodes`, `context`, `success`,
`modified_nodes`, `error`. When adapters are built (Wave 2-C), it is ambiguous which schema is law.
**Fix:** Reconcile to one schema and delete the other from the docs.

### M2 — `decode_task_bundle` produces a type-incorrect object
`decode_task_bundle` sets `locks=data.get("locks", [])` — a list of raw **dicts**, but `TaskBundle.locks`
is typed `list[LockEntry]`. `encode`→`decode` does not round-trip `locks`. **Fix:** Reconstruct
`LockEntry` (and its nested `ResourceRef`) on decode, or drop `locks` from the wire type deliberately.

### M3 — `AgentAdapter` ABC is subprocess-only but strategy is API-first
The ABC mandates `spawn(self, working_dir) -> subprocess.Popen`. PLANS §6 now declares API adapters
(Anthropic/OpenAI SDK) the *primary* path — they have no subprocess. Every API adapter will be forced
to implement a meaningless `spawn`. **Fix:** Split the interface — a minimal `AgentAdapter`
(`format_task`/`parse_result`/`health_check`) with subprocess methods on a `SubprocessAgentAdapter`
subclass.

### M4 — `mypy --strict` is NOT clean (README says it is)
`mypy --strict mak` reports 2 errors in `ingestion.py:78-79` (`"AST" has no attribute "lineno"/
"end_lineno"` — accessing line attributes on a bare `ast.AST` in the `else` branch). README §Status
claims *"mypy --strict clean on completed modules."* That claim is false today, and the error is a
latent bug (not all `ast.AST` nodes carry `lineno`). **Fix:** Narrow the type / guard the attribute
access; restore a green strict run.

### M5 — `ruff check` is NOT clean (139 errors; 13 in `mak/`)
`ruff check mak tests` reports 139 violations (D102/D101 missing docstrings, I001 unsorted imports,
E501 long lines, F401 unused imports) — 13 of them inside `mak/` itself. Wave 4's completion criterion
("`ruff check mak/` clean") is already violated and unenforced. **Fix:** `ruff check --fix`, hand-fix
docstring/line-length items, then gate in CI.

### M6 — Config drift and unsafe coercion
Documented keys are silently ignored: `lock_timeout_s`, `deadlock_check_interval_s`,
`pool_size_per_type`, `git.branch`, `node_store.parser`, and PLANS' `working_dir` (code reads
`work_dir`). `max_concurrent_agents` is parsed but unused. `int()/float()` casts on malformed values
raise bare `ValueError` instead of `ConfigError` (violates AGENTS.md). `bool(raw.get("auto_commit"))`
is a trap — `bool("false") is True`. **Fix:** Parse every documented key or remove it from docs; wrap
coercion in `ConfigError`; treat string booleans explicitly.

### M7 — NodeStore API diverges from its own design doc
PLANS §2.3 lists `put_node(id, frag, version)`, `commit_node(id, version)`,
`rollback_node(id, version)`, and `reconstruct_file` as a `NodeStore` method. The implementation drops
the `version` params and puts reconstruction in a free function. `get_committed_fragments` returns dict
order, not source order. **Fix:** Align signatures with the design (versioned) or update the design;
make reconstruction reachable from the store.

---

## 4. ⚪ Low — Process, Hygiene, Dead Code

- **L1 — No CI, no pre-commit.** Nothing enforces tests/mypy/ruff. On a codebase *explicitly built by
  parallel agents across waves*, the absence of an automated gate is the highest-leverage process gap.
  **Fix:** Add a GitHub Actions workflow (pytest + `mypy --strict` + `ruff check`) and a pre-commit hook.
- **L2 — Global mutable `ADAPTER_REGISTRY`.** A module-global dict mutated by `register_adapter`
  directly violates AGENTS.md "No global mutable state" and is a concurrency hazard (tests need
  `clear_registry`). **Fix:** Make it an injectable `AdapterRegistry` instance.
- **L3 — Build artifact committed.** `multi_agent_kernel.egg-info/` is tracked in git. **Fix:** Remove
  and add to `.gitignore`.
- **L4 — Authoritative docs are gitignored.** `.gitignore` excludes `PLANS.md`, `TASKS.md`, `AGENTS.md`,
  `CLAUDE.md` — the design of record is not version-controlled, so design history is unrecoverable and
  invisible to reviewers/CI. **Fix:** Track these (or move secrets out and track the rest).
- **L5 — SessionLogger is not concurrency-safe.** Opens the file per `log()` call with no lock/fsync;
  concurrent agents can interleave or truncate lines. **Fix:** Serialize writes (lock or single writer
  thread/queue); flush on write.
- **L6 — Dead/contradictory code + silent swallow.** `normalize_with_ast` (ast.unparse) is unused and
  contradicts the libcst decision; `format_with_ruff` silently swallows ruff failure and returns
  unformatted source (violates AGENTS.md error policy). **Fix:** Delete `normalize_with_ast`; log on
  ruff failure.
- **L7 — Deadlock detector rough edges.** `find_cycles` reports the same cycle once per rotation
  (duplicates) and recurses unbounded (stack overflow on deep graphs); `resolve` names a victim but no
  caller aborts it (no integration yet). **Fix:** Dedupe cycles canonically; convert DFS to iterative;
  wire `resolve` to actual abort when the scheduler lands.

---

## 5. Cross-Cutting Observations

- **Tests assert behavior, not properties.** 148 tests, but no **round-trip property test**
  (`ingest(f) → reconstruct → semantically_equals(f)`) — exactly the test that would have caught C1–C3.
  The lock tests are entirely single-threaded, so H1's race is untestable as written.
- **"Complete" overstates done-ness.** README marks `node_store` "Partial" but `lock_manager`
  "Complete" and Wave 1 "✅ COMPLETE." Given H2/H3 and the missing concurrency model, "Complete" should
  mean "API-complete, correctness-unverified." Recommend a stricter definition of done: green CI +
  property tests + documented concurrency invariant.
- **Documentation is ahead of code in load-bearing ways.** The README sells comment preservation
  (false today), method-level locking (unimplemented), and clean strict-typing/lint (false). Trim
  claims to what ships, or ship to match the claims — but don't let the gap persist, because it sets
  reviewer expectations the code doesn't meet.

---

## 6. Recommended Sequencing

1. **Wave H (Hardening) — before Wave 2.** Fix C1–C5, H1–H4; add the round-trip property test, a
   concurrency stress test, CI, and pre-commit. Re-green mypy/ruff. (See TASKS.md Wave H.)
2. **Reframe the libcst task** from "reconstruction normalization swap" to "CST-based ingest +
   reconstruct" so comments survive (C3).
3. **Only then** build Wave 2 (scheduler, conflict detector, API adapters) on a node store proven to
   round-trip and a lock layer with a stated, tested concurrency model.

The full remediation backlog lives in **PLANS.md §15** (Risk Register, IDs C1–L7) and **TASKS.md
Wave H** (agent-assigned tasks with files and acceptance criteria).
