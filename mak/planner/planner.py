"""Planner: decompose a user task into a validated ``SubTask`` DAG via an LLM.

The planner is the only module that calls an LLM for a plan. It builds a prompt
containing the user's task and the node inventory (never source), asks the model
for a JSON plan, and validates that JSON against the ``SubTask`` schema
(:mod:`mak.planner.plan_schema`) before accepting it. A malformed or
schema-invalid response is retried up to ``max_retries`` times — each retry
feeds the parse error back to the model — after which ``PlannerFailedError`` is
raised.

How much of the inventory a prompt shows is the ``strategy``:

- ``oneshot`` — every node id as a flat list, in one call (the compatibility
  mode: its prompt is byte-identical to what it has always been);
- ``outline`` — a file-level outline, then one detail call per step;
- ``full`` — every file at level 1 (ids, shapes, incoming references;
  :mod:`mak.planner.inventory`), in one call;
- ``retrieval`` — the repository tree plus a few pre-selected files, then up to
  ``max_expansions`` rounds in which the model may ask to see more
  (:mod:`mak.planner.expansion`, :mod:`mak.planner.retrieval`), all inside one
  cumulative ``inventory_token_budget``;
- ``auto`` — ``full`` when the whole level-1 view fits the budget, else
  ``retrieval``.

Prompts for ``full`` and ``retrieval`` are laid out as append-only *stable*
blocks plus a *volatile* tail (the round directive and any retry note), so a
backend that implements :class:`CachingPlannerLLM` can reuse the prefix across
retries, rounds, and different tasks in one session. Every call is reported as a
:class:`~mak.planner.telemetry.PlannerCall` while it happens.

The LLM is injected as a ``PlannerLLM`` (anything with ``complete(prompt) -> str``)
so the planner is testable with canned responses and is not bound to one SDK.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable

from mak.core.exceptions import PlannerFailedError
from mak.core.types import NodeId, SubTask
from mak.planner.expansion import (
    SEED_SHARE,
    ExpandRequest,
    PlanReply,
    PromptParts,
    RetrievalRounds,
    reply_outcome,
    run_retrieval,
)
from mak.planner.inventory import InventoryView
from mak.planner.outline import (
    assemble_outline,
    file_inventory,
    inventory_for_files,
    namespace_tasks,
    outline_listing,
    parse_outline,
)
from mak.planner.plan_schema import (
    _plan_to_json,
    is_python_target,
    parse_plan,
    target_file,
)
from mak.planner.response import ResponseError, TruncatedResponseError, loads_json
from mak.planner.retrieval import LexicalRetriever, Retriever
from mak.planner.telemetry import (
    CallRecorder,
    CallSite,
    InventoryStats,
    PlannerObserver,
    PlanningSummary,
    PlanOutcome,
    make_call,
)

__all__ = [
    "STRATEGIES",
    "CachingPlannerLLM",
    "Planner",
    "PlannerLLM",
    "is_python_target",
    "parse_plan",
    "target_file",
]

_T = TypeVar("_T")

STRATEGIES = ("auto", "oneshot", "outline", "full", "retrieval")
DEFAULT_INVENTORY_TOKEN_BUDGET = 12_000
DEFAULT_MAX_EXPANSIONS = 3

_PLAN_CORE = """\
You are the MAK planner. Decompose the user's task into the smallest set of \
independent sub-tasks that can run concurrently, with explicit dependency edges.

Respond with ONLY a JSON array (no prose, no code fences). Each element is an \
object with these keys:
  - "task_id": unique short string id for the sub-task
  - "description": what the sub-task should accomplish
  - "target_nodes": array of node ids this sub-task will WRITE (from the inventory \
below, or new ids for new symbols)
  - "context_nodes": array of node ids this sub-task needs to READ for context \
(sibling methods, class attributes, imports) but will not modify
  - "depends_on": array of task_ids that must complete before this one
  - "agent_type": the id of the agent to run this sub-task, from the \
CONFIGURED AGENTS list below (e.g. "anthropic_api", "nvidia-llama")

Optional interface declarations (MAK enforces each one when the task commits):
  - "changes_api": false when the task only changes function BODIES of its \
targets (it then runs in parallel with the tasks that call them, and a signature \
change is rejected); true when it changes a signature, return type, class fields, \
or deletes/renames a target; omit it when unsure
  - "api_targets": with changes_api true, the target ids whose API changes
  - "contract": {"<target id>": "def name(param: type, ...) -> ReturnType"} for \
every API this task creates or changes. Dependent tasks are built against it and \
the implementation must match it exactly
  - "registry_keys": {"<target id>": ["<key>", ...]} when the task only appends \
register("<key>", ...) lines to a shared registration function

MAK edits Python only: every target node id must name a Python source file — either \
"path/to/file.py" or "path/to/file.py::kind::qualified_name". Do NOT target \
non-Python files (no .md, .json, .txt, .js, .html, .css, README, or doc/architecture \
files) — MAK cannot represent them. If the task implies documentation or other \
non-Python artifacts, leave them out of the plan.

Decompose by FILE for a new project: give each new file its own sub-task with that \
file as a bare-path target ("pkg/foo.py"). Never have two sub-tasks both write the \
same whole file — that overwrites work. Prefer many small, focused modules over one \
giant file, and depend on a file only when you truly need its symbols. To split one \
file across sub-tasks, target individual symbols ("pkg/foo.py::function::name"); \
otherwise one file = one task.

Only assign two sub-tasks to write the same node if one depends on the other."""

_CASCADE_PREVENTION = """\
CRITICAL — CASCADE PREVENTION: If ANY sub-task changes a function's public
signature (rename, add, remove, or reorder parameters; change the return type or
default values), you MUST also include sub-tasks for EVERY node that calls that
function — even across different files. Scan the entire inventory before finalising
your plan. An incomplete plan that leaves callers with a stale signature forces a
costly follow-up wave; this is a planning failure. When uncertain whether a caller
exists, include a fix-up task anyway: a no-op task is far cheaper than a broken
codebase. Search the inventory for any node whose name suggests it calls a symbol
you are changing, and include it as a target."""

# The ``oneshot`` instructions: byte-for-byte what every oneshot prompt has sent.
# ``oneshot`` is the compatibility mode, so it keeps the paragraph asking the
# model to find callers itself even though the strategies that show the
# reference graph replace it.
_PLAN_INSTRUCTIONS = f"{_PLAN_CORE}\n\n{_CASCADE_PREVENTION}"

# The ``full`` and ``retrieval`` strategies show the reference graph, so the
# model no longer has to guess callers from names. With caller tasks on (the
# default) MAK adds the caller updates it can see itself.
_CALLERS_AUTO = """\
CALLERS: MAK knows the static call graph (the "← refs" column). When a task \
changes a function's or method's signature, set "changes_api": true and give its \
"contract"; MAK then adds caller-update tasks for every caller it can see, so do \
not add those yourself. Add caller tasks only for callers MAK cannot see: calls \
through self or an instance, dynamic dispatch (getattr, registries, callbacks), \
and code that other tasks in this plan create. When a task only changes function \
bodies, set "changes_api": false."""

_CALLERS_MANUAL = """\
CALLERS: when a task changes a function's or method's signature (rename, add, \
remove or reorder parameters, change the return type or defaults), you MUST also \
add tasks that update EVERY caller, even across files. Use the "← refs" column to \
find them, and remember it cannot see calls through self or an instance, dynamic \
dispatch (getattr, registries, callbacks), or code that other tasks in this plan \
create. Set "changes_api": true and give the "contract" for every signature you \
change; when a task only changes function bodies, set "changes_api": false."""

_INVENTORY_FORMAT = """\
READING THE INVENTORY: a file is a header line ("path/to/file.py  (N nodes · used \
by M other files)") followed by one line per node. Each node line starts with the \
id SUFFIX "::kind::name"; the node's full id is the file path immediately followed \
by that suffix — "pkg/a.py" and "::function::load" make "pkg/a.py::function::load". \
Always write FULL ids in the plan. After the suffix comes the node's shape (its def \
or class line; default values are shown as "=...") and then "← N refs · M files \
(...)": the static references MAK found to that node and the files they come \
from. A file held as one whole-file node shows "(whole-file node ...)"; target it \
by its path."""

_RETRIEVAL_PROTOCOL = """\
RETRIEVAL: you are shown the repository TREE (directories and files with node \
counts) and the FILE DETAIL of files MAK pre-selected for this task — not every \
file. Before planning you may ask to see more: reply with ONLY \
{{"expand": ["pkg/a.py", "pkg/b/"], "why": "<optional reason>"}} instead of a \
plan. A file path shows that file's nodes; a directory path shows its contents one \
level deeper. You may ask at most {rounds} time(s), and MAK says when expansion is \
closed. Never put "expand" and a plan in one reply. Do not target an id in a file \
you have not seen in detail unless it is a genuinely NEW symbol: expand the file \
first."""

_RETRIEVAL_NO_EXPANSION = """\
RETRIEVAL: you are shown the repository TREE (directories and files with node \
counts) and the FILE DETAIL of files MAK pre-selected for this task — not every \
file, and you cannot ask for more. Target an id in a file you have not seen in \
detail only when it is a genuinely NEW symbol."""


_OUTLINE_INSTRUCTIONS = """\
You are the MAK planner working in OUTLINE mode. Sketch the task at the FILE level \
first — a short ordered list of steps, each naming the files it touches. Detail comes \
in a later pass, so keep each step coarse.

Respond with ONLY a JSON array (no prose, no code fences). Each element is an object \
with these keys:
  - "step_id": unique short string id for the step
  - "description": what this step accomplishes (a later pass expands it into tasks)
  - "files": array of file paths this step touches (from the inventory below, or new \
".py" paths for new files)
  - "depends_on": array of step_ids that must complete before this one

Keep steps independent where you can; only add a "depends_on" edge when a step truly \
needs another step's files to exist or be updated first. Do NOT list non-Python \
files."""

_CRITIQUE_INSTRUCTIONS = """\
You are reviewing a MAK plan you just produced. Look for three defects: missed \
dependency edges (a task edits a node another task's node calls, with no depends_on \
between them), hallucinated node ids (a target that does not match the real code), \
and needless serialization (a depends_on edge with no real code reason).

If the plan is already good, respond with EXACTLY this JSON object and nothing else:
  {"verdict": "ok"}
Otherwise respond with ONLY the corrected full plan as a JSON array in the SAME schema \
as before (task_id, description, target_nodes, context_nodes, depends_on, agent_type, \
and any changes_api / api_targets / contract / registry_keys a task declared). \
Do not add prose or code fences."""


class PlannerLLM(Protocol):
    """Minimal LLM interface the planner needs: a prompt-in, text-out call."""

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        ...


@runtime_checkable
class CachingPlannerLLM(Protocol):
    """A ``PlannerLLM`` that can be told which part of a prompt is stable.

    ``stable`` blocks are append-only across one plan's retries and rounds, so a
    provider that caches prefixes can reuse them; ``volatile`` is the part that
    changes on every attempt. The prompt is ``"".join(stable) + volatile`` either
    way — an LLM without this method is sent exactly that string.
    """

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        ...

    def complete_parts(self, stable: Sequence[str], volatile: str) -> str:
        """Return the completion for ``"".join(stable) + volatile``."""
        ...


_TRUNCATION_NOTE = """\
Your previous response was cut off by the output-token limit before the JSON \
closed, so it could not be read. Produce a SMALLER plan that fits in one \
response: keep every "description" to one short sentence, merge sub-tasks that \
touch the same file, and emit compact JSON (no pretty-printing, no blank lines, \
no trailing prose). Return ONLY the corrected JSON."""

# Enough of a pause to clear a per-minute rate limit without stalling a run that
# is failing for a reason waiting will not fix.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 8.0


def _backoff_seconds(attempt: int) -> float:
    """Return the delay before retry ``attempt`` (1-based), capped."""
    return float(min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS))


def _retry_note(error: Exception) -> str:
    """Return the feedback appended to the prompt after a failed attempt.

    A truncated response is the one failure that repeats verbatim on a naive
    retry — the same request yields the same over-long plan and the same cut — so
    it gets a note that asks for a smaller plan instead of a corrected one.
    """
    if isinstance(error, TruncatedResponseError):
        return _TRUNCATION_NOTE
    return (
        f"Your previous response was rejected: {error}\n"
        "Return ONLY the corrected JSON."
    )


def _failure_hint(error: Exception | None) -> str:
    """Return actionable advice to append when the retry budget runs out."""
    if isinstance(error, TruncatedResponseError):
        return (
            ". The plan did not fit in the planner model's output budget — narrow "
            "the task, or pick a planner model with a larger output limit."
        )
    return ""


class Planner:
    """Turns a natural-language task into a validated list of ``SubTask``.

    ``strategy`` decides how much of the inventory a prompt shows (see the module
    docstring); the constructor's default stays ``oneshot`` so a ``Planner``
    built directly behaves as it always has, while the configuration's default
    is ``auto``.
    """

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_retries: int = 3,
        agent_types: list[str] | None = None,
        agent_labels: list[str] | None = None,
        strategy: str = "oneshot",
        self_critique: bool = False,
        inventory_token_budget: int = DEFAULT_INVENTORY_TOKEN_BUDGET,
        max_expansions: int = DEFAULT_MAX_EXPANSIONS,
        auto_caller_tasks: bool = True,
        retriever: Retriever | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown planner strategy {strategy!r}; use {STRATEGIES}")
        self._llm = llm
        self._max_retries = max_retries
        # The agent *ids* actually configured for this run, so the plan can name
        # a real one in each task's "agent_type" instead of guessing (an
        # unconfigured id would otherwise have to be remapped by the session).
        # The serialized field keeps its name for compatibility; its value is a
        # routing id.
        self._agent_types = list(agent_types or [])
        # Optional human labels — "nvidia-llama — meta/llama-3.3-70b via NVIDIA
        # Build" — so the model can choose sensibly between several agents. They
        # carry the model and the endpoint's display name and deliberately carry
        # **no** URL, header or credential variable: this string is sent to a
        # model, and an internal hostname in it is a leak with no planning value.
        self._agent_labels = list(agent_labels or [])
        self._strategy = strategy
        self._self_critique = self_critique
        self._budget = inventory_token_budget
        self._max_expansions = max_expansions
        self._auto_caller_tasks = auto_caller_tasks
        self._retriever: Retriever = retriever or LexicalRetriever()
        # Tokens this planner has spent, summed across every call it makes —
        # including the retries, the expansion rounds and the optional critique
        # pass, which are real spend and were previously invisible.
        self.token_usage: Counter[str] = Counter()

    @property
    def strategy(self) -> str:
        """The configured strategy (``auto`` is resolved per plan)."""
        return self._strategy

    # -- prompts ----------------------------------------------------------

    def _agents_block(self) -> str:
        if not self._agent_types:
            return ""
        listed = self._agent_labels or self._agent_types
        return (
            "\nCONFIGURED AGENTS (set each task's \"agent_type\" to one of "
            "these ids, or leave it empty to let MAK distribute the work):\n"
            + "\n".join(f"  - {t}" for t in listed)
            + "\n"
        )

    def _build_prompt(self, user_task: str, node_inventory: list[NodeId]) -> str:
        inventory = "\n".join(f"  - {nid}" for nid in node_inventory) or "  (empty)"
        return (
            f"{_PLAN_INSTRUCTIONS}\n\n"
            f"USER TASK:\n{user_task}\n\n"
            f"NODE INVENTORY (qualified names you may target):\n{inventory}\n"
            f"{self._agents_block()}"
        )

    def _view_head(self, strategy: str) -> str:
        """Instructions and roster: the part of S1 before the inventory."""
        callers = _CALLERS_AUTO if self._auto_caller_tasks else _CALLERS_MANUAL
        parts = [_PLAN_CORE, callers, _INVENTORY_FORMAT]
        if strategy == "retrieval":
            parts.append(
                _RETRIEVAL_PROTOCOL.format(rounds=self._max_expansions)
                if self._max_expansions
                else _RETRIEVAL_NO_EXPANSION
            )
        return "\n\n".join(parts) + "\n" + self._agents_block()

    # -- planning ---------------------------------------------------------

    def decompose(
        self, user_task: str, node_inventory: list[NodeId]
    ) -> list[SubTask]:
        """Decompose ``user_task`` into sub-tasks, retrying on invalid LLM output.

        The plan :meth:`plan` produces, without its telemetry.
        """
        return self.plan(user_task, node_inventory).plan

    def plan(
        self,
        user_task: str,
        node_inventory: list[NodeId],
        *,
        view: InventoryView | None = None,
        observer: PlannerObserver | None = None,
    ) -> PlanOutcome:
        """Plan ``user_task`` and report what every call cost.

        ``view`` is the inventory's hierarchical view (the session caches one per
        store generation; without it one is built from the ids alone, with no
        shapes or references). ``observer`` receives each :class:`PlannerCall`
        as it finishes, so a plan that fails still reports what it spent.
        Whatever the strategy, the plan is validated by ``parse_plan``, and when
        ``self_critique`` is set one reflection pass may replace it.
        """
        recorder = CallRecorder(observer)
        strategy = self._strategy
        seen: frozenset[str] | None = None
        if strategy == "outline":
            plan, rounds = self._decompose_outline(user_task, node_inventory, recorder)
        elif strategy == "oneshot":
            plan, rounds = self._plan_oneshot(user_task, node_inventory, recorder), 1
        else:
            shown = view if view is not None else InventoryView(node_inventory)
            if strategy == "auto":
                fits = shown.full_tokens() <= self._budget
                strategy = "full" if fits else "retrieval"
            if strategy == "full":
                plan, rounds = self._plan_full(user_task, shown, recorder), 1
            else:
                plan, seen, rounds = self._plan_retrieval(user_task, shown, recorder)
        if self._self_critique:
            plan = self._critique_plan(plan, recorder, strategy)
        calls = tuple(recorder.calls)
        return PlanOutcome(
            plan=plan,
            strategy=strategy,
            seen_files=seen,
            calls=calls,
            summary=PlanningSummary.of(calls, rounds),
        )

    def _plan_oneshot(
        self, user_task: str, node_inventory: list[NodeId], recorder: CallRecorder
    ) -> list[SubTask]:
        """One call over the flat id listing (today's prompt, byte for byte)."""
        prompt = self._build_prompt(user_task, node_inventory)
        site = CallSite("plan", 0, "oneshot", _flat_stats(node_inventory))
        return self._complete_with_retries(
            prompt, parse_plan, site=site, recorder=recorder
        )

    def _plan_full(
        self, user_task: str, view: InventoryView, recorder: CallRecorder
    ) -> list[SubTask]:
        """One call over every file at level 1 (shapes and references)."""
        inventory = view.render_full()
        files = len(view.files)
        stable = (
            f"{self._view_head('full')}\nNODE INVENTORY ({files:,} files, "
            f"{view.node_count:,} nodes; every file, one line per node):\n"
            f"{inventory}\n",
            f"\nUSER TASK:\n{user_task}\n",
        )
        stats = InventoryStats(
            files_total=files,
            files_shown=files,
            nodes_shown=view.node_count,
            chars=len(inventory),
        )
        return self._complete_with_retries(
            PromptParts(stable),
            parse_plan,
            site=CallSite("plan", 0, "full", stats),
            recorder=recorder,
        )

    def _plan_retrieval(
        self, user_task: str, view: InventoryView, recorder: CallRecorder
    ) -> tuple[list[SubTask], frozenset[str], int]:
        """Show the tree and seeds, then run the expansion rounds the model asks for."""
        seeds = self._retriever.seed(
            user_task, view, int(self._budget * SEED_SHARE)
        )
        rounds = RetrievalRounds(
            view,
            head=self._view_head("retrieval"),
            user_task=user_task,
            seeds=seeds,
            budget_tokens=self._budget,
            max_expansions=self._max_expansions,
        )

        def complete(
            parts: PromptParts,
            parse: Callable[[str], PlanReply | ExpandRequest],
            site: CallSite,
        ) -> PlanReply | ExpandRequest:
            return self._complete_with_retries(
                parts, parse, site=site, recorder=recorder, outcome_of=reply_outcome
            )

        plan = run_retrieval(rounds, complete)
        return plan, rounds.seen_files, rounds.rounds

    def _critique_plan(
        self,
        plan: list[SubTask],
        recorder: CallRecorder | None = None,
        strategy: str | None = None,
    ) -> list[SubTask]:
        """Run one reflection pass; adopt a corrected plan or keep the original.

        A broken critique must never break a good plan: only a ``verdict: ok`` reply
        or a plan that re-parses cleanly is honored — anything else keeps ``plan``.
        No retry budget is consumed.
        """
        prompt = f"{_CRITIQUE_INSTRUCTIONS}\n\nPLAN:\n{_plan_to_json(plan)}\n"
        recorder = recorder or CallRecorder(None)
        site = CallSite("critique", 0, strategy or self._strategy, InventoryStats())
        attempt = _Attempt(
            site, PromptParts((prompt,)), 1, getattr(self._llm, "last_usage", None)
        )
        # The critique is an optional improvement pass, so *any* failure in it —
        # a dead API, a truncated reply, an unparseable plan — must leave the
        # already-valid plan standing rather than take the run down.
        try:
            raw = self._send(attempt.parts)
        except Exception:  # noqa: BLE001 - see comment above
            self._finish(attempt, recorder, answered=False, outcome="call_failed")
            return plan
        try:
            data = loads_json(raw)
            revised = None if _is_ok_verdict(data) else parse_plan(raw)
        except ValueError:
            self._finish(attempt, recorder, answered=True, outcome="rejected")
            return plan
        self._finish(attempt, recorder, answered=True, outcome="plan")
        return plan if revised is None else revised

    # -- one round's calls ------------------------------------------------

    def _send(self, parts: PromptParts) -> str:
        """Call the LLM, telling a caching backend which blocks are stable."""
        if isinstance(self._llm, CachingPlannerLLM):
            return self._llm.complete_parts(parts.stable, parts.volatile)
        return self._llm.complete(parts.text)

    def _finish(
        self,
        attempt: _Attempt,
        recorder: CallRecorder,
        *,
        answered: bool,
        outcome: str,
    ) -> None:
        """Fold the call's usage into the totals and report it."""
        usage = self._take_usage(attempt.usage_before, answered=answered)
        recorder.record(make_call(
            attempt.site,
            attempt=attempt.number,
            prompt_chars=len(attempt.parts.text),
            stable_chars=sum(len(block) for block in attempt.parts.stable),
            usage=usage,
            outcome=outcome,
            duration_ms=(time.perf_counter() - attempt.started) * 1000.0,
        ))

    def _take_usage(self, before: object, *, answered: bool) -> dict[str, int]:
        """Return (and total) the usage the backend reported for the last call.

        A call that raised is billed only when the backend recorded a fresh
        usage for it — a truncated reply was still generated and paid for —
        never with the usage left over from the call before it.
        """
        usage = getattr(self._llm, "last_usage", None)
        if not isinstance(usage, dict) or (not answered and usage is before):
            return {}
        counted = {
            k: v for k, v in usage.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        self.token_usage.update(counted)
        return counted

    def _complete_with_retries(
        self,
        prompt: str | PromptParts,
        parse: Callable[[str], _T],
        *,
        site: CallSite | None = None,
        recorder: CallRecorder | None = None,
        outcome_of: Callable[[_T], str] | None = None,
    ) -> _T:
        """Call the LLM until ``parse`` accepts a response, feeding back errors.

        Both halves of an attempt are retried: a transient provider failure (a
        rate limit, a dropped connection) is as recoverable as a malformed reply,
        and previously it aborted the run outright with the retry budget untouched.
        Only a failed *call* backs off — a rejected plan is re-asked immediately,
        since waiting does nothing to make the model answer better.

        The retry note goes into the prompt's volatile part only, so every
        attempt re-sends the same stable blocks. Each attempt is reported to
        ``recorder`` as one :class:`PlannerCall` at ``site``.
        """
        parts = prompt if isinstance(prompt, PromptParts) else PromptParts((prompt,))
        site = site or CallSite("plan", 0, self._strategy, InventoryStats())
        recorder = recorder or CallRecorder(None)
        last_error: Exception | None = None
        call_failed = False
        for index in range(self._max_retries):
            if call_failed:
                self._sleep(_backoff_seconds(index))
            volatile = parts.volatile
            if last_error is not None:
                volatile = f"{volatile}\n{_retry_note(last_error)}"
            attempt = _Attempt(site, PromptParts(parts.stable, volatile), index + 1,
                               getattr(self._llm, "last_usage", None))
            try:
                raw = self._send(attempt.parts)
            except PlannerFailedError:
                # A setup failure (missing SDK, unknown backend) is not transient;
                # retrying it just delays the same message.
                self._finish(attempt, recorder, answered=False, outcome="call_failed")
                raise
            except ResponseError as exc:
                # A provider-signalled bad response (a cut, a blocked candidate)
                # is a response problem, not a transport one — no backoff.
                self._finish(attempt, recorder, answered=False,
                             outcome=_failure_outcome(exc))
                last_error, call_failed = exc, False
                continue
            except Exception as exc:  # noqa: BLE001 - provider SDKs raise freely
                self._finish(attempt, recorder, answered=False, outcome="call_failed")
                last_error, call_failed = exc, True
                continue
            call_failed = False
            try:
                result = parse(raw)
            except ValueError as exc:
                self._finish(attempt, recorder, answered=True,
                             outcome=_failure_outcome(exc))
                last_error = exc
                continue
            outcome = outcome_of(result) if outcome_of is not None else "plan"
            self._finish(attempt, recorder, answered=True, outcome=outcome)
            return result
        raise PlannerFailedError(
            f"planner failed to produce a valid plan after {self._max_retries} "
            f"attempts: {last_error}{_failure_hint(last_error)}"
        )

    @staticmethod
    def _sleep(seconds: float) -> None:
        """Pause between attempts (a seam so tests do not wait)."""
        time.sleep(seconds)

    # -- outline ----------------------------------------------------------

    def _decompose_outline(
        self, user_task: str, node_inventory: list[NodeId], recorder: CallRecorder
    ) -> tuple[list[SubTask], int]:
        """Two-pass plan: a file-level outline, then per-step symbol detail."""
        listed = outline_listing(node_inventory)
        files = len(file_inventory(node_inventory))
        outline_site = CallSite("outline", 0, "outline", InventoryStats(
            files_total=files, files_shown=files, nodes_shown=len(node_inventory),
            chars=len(listed),
        ))
        steps = self._complete_with_retries(
            self._build_outline_prompt(user_task, node_inventory),
            parse_outline,
            site=outline_site,
            recorder=recorder,
        )

        step_tasks: dict[str, list[SubTask]] = {}
        for index, step in enumerate(steps):
            restricted = inventory_for_files(node_inventory, step.files)
            detail_prompt = self._build_prompt(step.description, restricted)
            stats = _flat_stats(restricted, files_total=files)
            tasks = self._complete_with_retries(
                detail_prompt,
                parse_plan,
                site=CallSite("detail", index + 1, "outline", stats),
                recorder=recorder,
            )
            step_tasks[step.step_id] = namespace_tasks(tasks, index)

        merged = assemble_outline(steps, step_tasks)
        # Re-run the full-plan invariants (duplicate ids, whole-file owners) over the
        # assembled result the same way an LLM plan would be checked.
        return parse_plan(_plan_to_json(merged)), 1 + len(steps)

    def _build_outline_prompt(
        self, user_task: str, node_inventory: list[NodeId]
    ) -> str:
        return (
            f"{_OUTLINE_INSTRUCTIONS}\n\n"
            f"USER TASK:\n{user_task}\n\n"
            "FILE INVENTORY (files you may touch, with their symbols):\n"
            f"{outline_listing(node_inventory)}\n"
        )


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One call in flight: where it sits, what it sends, and when it started."""

    site: CallSite
    parts: PromptParts
    number: int
    usage_before: object = None
    started: float = field(default_factory=time.perf_counter)


def _failure_outcome(error: Exception) -> str:
    """Return the ``PlannerCall.outcome`` of a rejected reply."""
    return "truncated" if isinstance(error, TruncatedResponseError) else "rejected"


def _is_ok_verdict(data: object) -> bool:
    return isinstance(data, dict) and data.get("verdict") == "ok"


def _flat_stats(
    node_inventory: list[NodeId], *, files_total: int | None = None
) -> InventoryStats:
    """Stats for a prompt that lists every id in ``node_inventory``."""
    files = len({target_file(n) for n in node_inventory})
    return InventoryStats(
        files_total=files if files_total is None else files_total,
        files_shown=files,
        nodes_shown=len(node_inventory),
        chars=len("\n".join(f"  - {nid}" for nid in node_inventory)),
    )

