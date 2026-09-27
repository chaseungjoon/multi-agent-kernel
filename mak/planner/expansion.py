"""The ``retrieval`` strategy's rounds: the model may ask to see more.

A ``retrieval`` prompt shows the repository tree and a few pre-selected files in
detail, not the whole inventory. One reply is then either a plan or an expand
request — ``{"expand": ["pkg/a.py", "pkg/b/"], "why": "…"}`` — and each request
buys one more round with the asked-for files (level 1) or directories (one tree
level deeper) appended to the prompt.

Three things keep this bounded and honest:

- **rounds** — every round counts toward ``max_expansions``, including one that
  only asked for paths already shown or not found. When they run out the
  prompt says "return the plan now", and a further expand reply is rejected
  like any malformed reply;
- **budget** — ``inventory_token_budget`` bounds the inventory *cumulatively*
  across the tree, the seeds and every expansion of one plan. A request that
  does not fit is listed as not expanded, never silently dropped;
- **verification** — a plan that targets a non-existent id in a file the model
  never saw is shown that file and asked again while rounds remain; otherwise
  validation flags the target (``unseen_target``).

The prompt is kept **append-only**: each round's stable blocks are a byte prefix
of the next round's, and only the closing directive (plus any retry note)
changes between attempts — which is what lets a provider serve the prefix from
its cache.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mak.agent_runner.adapters.ollama_api_adapter import estimate_tokens
from mak.core.types import SubTask
from mak.planner.inventory import InventoryView
from mak.planner.plan_schema import parse_plan, target_file
from mak.planner.response import loads_json
from mak.planner.retrieval import SeedResult
from mak.planner.telemetry import CallSite, InventoryStats

# Shares of ``inventory_token_budget`` (module constants, not configuration).
# The tree gets a quarter; the seeds a bit more; whatever the model asks for
# after reading the tree gets the rest, because that is a better signal than a
# lexical guess. One file never takes more than a quarter.
TREE_SHARE = 0.25
SEED_SHARE = 0.35
FILE_SHARE = 0.25
# Below this many tokens of budget left, no expansion could show anything.
_MIN_EXPANSION_TOKENS = 32

CLOSED_DIRECTIVE = "\nDo not ask to expand further. Return the plan now.\n"
CLOSED_ERROR = "expansion is closed; return the plan"
VERIFY_NOTE = (
    "Your plan targets ids in files you had not seen; they are shown below; "
    "return the corrected plan."
)


@dataclass(frozen=True, slots=True)
class PromptParts:
    """A prompt split into its append-only ``stable`` blocks and the rest.

    The prompt is ``"".join(stable) + volatile``. A retry note is appended to
    ``volatile`` only, so every attempt of a round shares the stable bytes.
    """

    stable: tuple[str, ...]
    volatile: str = ""

    @property
    def text(self) -> str:
        """The whole prompt."""
        return "".join(self.stable) + self.volatile


@dataclass(frozen=True, slots=True)
class ExpandRequest:
    """A reply asking to see more of the inventory before planning."""

    paths: tuple[str, ...]
    why: str = ""


@dataclass(frozen=True, slots=True)
class PlanReply:
    """A reply that is a plan."""

    plan: list[SubTask]


def parse_reply(raw: str) -> PlanReply | ExpandRequest:
    """Parse one reply: a plan (array or ``{"subtasks": …}``) or an expand request.

    A reply carrying both an ``expand`` list and anything else is a
    ``ValueError`` — the normal retry note tells the model to pick one.
    """
    data = loads_json(raw)
    if not (isinstance(data, dict) and "expand" in data):
        return PlanReply(parse_plan(raw))
    extra = sorted(set(data) - {"expand", "why"})
    if extra:
        raise ValueError(
            "a reply is either a plan or an expand request, not both "
            f"(it also had {', '.join(extra)}); send one of them"
        )
    paths = data["expand"]
    if not isinstance(paths, list) or not paths or not all(
        isinstance(p, str) and p.strip() for p in paths
    ):
        raise ValueError("'expand' must be a non-empty list of file or directory paths")
    why = data.get("why", "")
    if not isinstance(why, str):
        raise ValueError("'why' must be a string")
    return ExpandRequest(tuple(dict.fromkeys(p.strip() for p in paths)), why.strip())


def parse_closed_reply(raw: str) -> PlanReply | ExpandRequest:
    """Parse a reply once expansion is closed: only a plan is accepted."""
    reply = parse_reply(raw)
    if isinstance(reply, ExpandRequest):
        raise ValueError(CLOSED_ERROR)
    return reply


def reply_outcome(reply: object) -> str:
    """Return the ``PlannerCall.outcome`` of a parsed reply."""
    return "expand" if isinstance(reply, ExpandRequest) else "plan"


def _normalize(path: str) -> str:
    """Strip whitespace, a leading ``./`` and any ``::kind::name`` suffix."""
    text = path.strip()
    while text.startswith("./"):
        text = text[2:]
    return target_file(text)


class RetrievalRounds:
    """One ``retrieval`` plan's prompt, budget and record of what was shown.

    ``head`` is the stable prompt prefix before the inventory (instructions and
    the agent roster); the tree follows it in the first block, and the task and
    seed files form the second. Every expansion or verification appends one
    block.
    """

    def __init__(
        self,
        view: InventoryView,
        *,
        head: str,
        user_task: str,
        seeds: SeedResult,
        budget_tokens: int,
        max_expansions: int,
        strategy: str = "retrieval",
    ) -> None:
        self._view = view
        self._budget = budget_tokens
        self._max_expansions = max_expansions
        self._strategy = strategy
        self._inventory = {str(n) for n in view.inventory}
        self._file_cap = max(1, int(budget_tokens * FILE_SHARE))
        self._shown: list[str] = []
        self._nodes_shown = 0
        self._used = 0
        self._chars = 0
        self._truncated = 0
        self._expanded_paths = 0
        self._rounds = 0
        self._seeds = 0
        self._phase = "plan"
        tree = view.render_tree(int(budget_tokens * TREE_SHARE))
        self._open_dirs = tree.expanded
        self._omitted = tree.omitted
        self._account(tree.text)
        self._stable = [head + self._tree_block(tree.text, tree.collapsed)]
        self._stable.append(self._task_block(user_task, seeds))

    # -- blocks -----------------------------------------------------------

    def _tree_block(self, text: str, collapsed: int) -> str:
        note = f"; {collapsed:,} directories collapsed" if collapsed else ""
        return (
            f"\nREPOSITORY TREE ({len(self._view.files):,} files, "
            f"{self._view.node_count:,} nodes{note}):\n{text or '  (empty)'}\n"
        )

    def _task_block(self, user_task: str, seeds: SeedResult) -> str:
        cap = min(self._file_cap, max(1, int(self._budget * SEED_SHARE)))
        rendered = [self._show_file(path, cap) for path in seeds.files]
        detail = "\n".join(text for text in rendered if text)
        self._seeds = sum(1 for text in rendered if text)
        block = f"\nUSER TASK:\n{user_task}\n"
        if detail:
            block += (
                "\nFILE DETAIL (files MAK pre-selected for this task):\n"
                f"{detail}\n"
            )
        return block

    def _account(self, text: str) -> None:
        """Charge one inventory rendering to the budget."""
        self._used += estimate_tokens(text)
        self._chars += len(text)

    def _show_file(self, path: str, cap: int) -> str:
        """Render ``path`` at level 1 if it fits the budget; "" when it does not."""
        rendered = self._view.render_file(path, cap)
        if self._used + estimate_tokens(rendered.text) > self._budget:
            return ""
        self._account(rendered.text)
        self._shown.append(path)
        self._nodes_shown += rendered.nodes_shown
        self._truncated += rendered.nodes_hidden
        return rendered.text

    # -- state ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Whether the model may no longer ask to expand."""
        return (
            self._rounds >= self._max_expansions
            or self._budget - self._used < _MIN_EXPANSION_TOKENS
        )

    @property
    def seen_files(self) -> frozenset[str]:
        """Files the model has been shown at level 1."""
        return frozenset(self._shown)

    @property
    def rounds(self) -> int:
        """Rounds the plan has taken so far, counting the one in progress."""
        return self._rounds + 1

    @property
    def used_tokens(self) -> int:
        """Estimated inventory tokens shown so far (never above the budget)."""
        return self._used

    def parts(self) -> PromptParts:
        """Return the prompt for the next call of this round."""
        if self.closed:
            directive = CLOSED_DIRECTIVE
        else:
            left = self._max_expansions - self._rounds
            directive = (
                "\nReply with the JSON plan, or with an expand request to see more "
                f"first ({left} expansion round{'' if left == 1 else 's'} left).\n"
            )
        return PromptParts(tuple(self._stable), directive)

    def parser(self) -> Callable[[str], PlanReply | ExpandRequest]:
        """Return this round's reply parser (expansion refused once closed)."""
        return parse_closed_reply if self.closed else parse_reply

    def site(self) -> CallSite:
        """Describe the next call for telemetry."""
        return CallSite(
            phase=self._phase,
            round=self._rounds,
            strategy=self._strategy,
            inventory=InventoryStats(
                files_total=len(self._view.files),
                files_shown=len(self._shown),
                nodes_shown=self._nodes_shown,
                chars=self._chars,
                collapsed_dirs=self._view.collapsed_count(self._open_dirs)
                + self._omitted,
                symbols_truncated=self._truncated,
                seed_files=self._seeds,
                expanded_paths=self._expanded_paths,
            ),
        )

    # -- rounds -----------------------------------------------------------

    def expand(self, request: ExpandRequest) -> None:
        """Answer an expand request with one appended block; spend a round."""
        lines = [f"\nEXPANSION {self._rounds + 1} (you asked for: "
                 f"{', '.join(request.paths)}):"]
        status: dict[str, list[str]] = {
            "already shown": [], "not found": [], "not a Python file": [],
            "not expanded (inventory budget reached)": [],
        }
        for raw in request.paths:
            shown = self._expand_one(_normalize(raw), status)
            if shown:
                lines.append(shown)
        for label, paths in status.items():
            if paths:
                lines.append(f"{label}: {'; '.join(paths)}")
        self._truncated += len(status["not expanded (inventory budget reached)"])
        self._stable.append("\n".join(lines) + "\n")
        self._rounds += 1
        self._phase = "expand"

    def _expand_one(self, path: str, status: dict[str, list[str]]) -> str:
        """Render one requested path, or record why it was not shown."""
        if self._view.file(path) is not None:
            if path in self._shown:
                status["already shown"].append(path)
                return ""
            text = self._show_file(path, self._file_cap)
            if not text:
                status["not expanded (inventory budget reached)"].append(path)
            else:
                self._expanded_paths += 1
            return text
        key = self._view.dir_key(path)
        if key is not None:
            return self._expand_dir(key, path, status)
        if not path.endswith("/") and not path.endswith(".py"):
            status["not a Python file"].append(path)
            return ""
        close = self._view.close_files(path)
        hint = f" (close: {', '.join(close)})" if close else ""
        status["not found"].append(f"{path}{hint}")
        return ""

    def _expand_dir(self, key: str, path: str, status: dict[str, list[str]]) -> str:
        opened = self._view.render_dir(key, self._open_dirs)
        if opened is None:
            status["already shown"].append(path)
            return ""
        text, open_dirs = opened
        if self._used + estimate_tokens(text) > self._budget:
            status["not expanded (inventory budget reached)"].append(path)
            return ""
        self._account(text)
        self._open_dirs = open_dirs
        self._expanded_paths += 1
        return text

    def unseen_targets(self, plan: list[SubTask]) -> list[str]:
        """Existing files the plan targets new ids in without having seen them."""
        files: set[str] = set()
        for task in plan:
            for node in task.target_nodes:
                text = str(node)
                if text in self._inventory:
                    continue  # a real id is real, seen or not
                path = target_file(text)
                if self._view.file(path) is not None and path not in self._shown:
                    files.add(path)
        return sorted(files)

    def verify(self, files: list[str], plan: list[SubTask]) -> bool:
        """Show ``files`` and ask for a corrected plan; False if none fit."""
        rendered = [self._show_file(path, self._file_cap) for path in files]
        shown = [text for text in rendered if text]
        if not shown:
            return False
        targets = sorted({
            str(node) for task in plan for node in task.target_nodes
            if target_file(str(node)) in files and str(node) not in self._inventory
        })
        missing = [path for path, text in zip(files, rendered, strict=True) if not text]
        lines = [f"\nVERIFY: {VERIFY_NOTE}", f"Unseen targets: {', '.join(targets)}"]
        lines.extend(shown)
        if missing:
            lines.append(
                "not expanded (inventory budget reached): " + "; ".join(missing)
            )
            self._truncated += len(missing)
        self._stable.append("\n".join(lines) + "\n")
        self._rounds += 1
        self._phase = "verify"
        return True


def run_retrieval(
    rounds: RetrievalRounds,
    complete: Callable[
        [PromptParts, Callable[[str], PlanReply | ExpandRequest], CallSite],
        PlanReply | ExpandRequest,
    ],
) -> list[SubTask]:
    """Drive the rounds until a plan is accepted.

    ``complete`` is one round's retry loop (``Planner._complete_with_retries``):
    a malformed reply inside a round is retried there, while an expansion or a
    verification is progress and starts the next round. Every round spends one
    of ``max_expansions``, so the loop ends after at most that many plus one.
    """
    while True:
        reply = complete(rounds.parts(), rounds.parser(), rounds.site())
        if isinstance(reply, ExpandRequest):
            rounds.expand(reply)
            continue
        unseen = rounds.unseen_targets(reply.plan)
        if unseen and not rounds.closed and rounds.verify(unseen, reply.plan):
            continue
        return reply.plan

