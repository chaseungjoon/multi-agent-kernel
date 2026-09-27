"""Wave 7: planner strategies, the expansion protocol, prompt parts, telemetry."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import pytest

from mak.core.exceptions import PlannerFailedError
from mak.core.types import NodeId
from mak.planner.expansion import CLOSED_DIRECTIVE, CLOSED_ERROR, parse_reply
from mak.planner.inventory import InventoryView
from mak.planner.planner import Planner
from mak.planner.response import TruncatedResponseError
from mak.planner.telemetry import PlannerCall, PlanOutcome
from tests.planner.fixture_repo import view_of
from tests.planner.synthetic_repo import INVOICE_FILE, SyntheticRepo

GOLDEN = Path(__file__).parent / "golden"
TASK = "Round `invoice_total` to cents"
REAL_TARGET = f"{INVOICE_FILE}::function::invoice_total"


def _plan(*targets: str, task_id: str = "t") -> str:
    return json.dumps([{
        "task_id": task_id, "description": "do it", "target_nodes": list(targets),
    }])


def _expand(*paths: str) -> str:
    return json.dumps({"expand": list(paths), "why": "need it"})


class Stub:
    """A PlannerLLM with only ``complete``: scripted replies, prompts recorded."""

    def __init__(self, replies: Sequence[str | Exception]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class PartsStub(Stub):
    """A CachingPlannerLLM double that records each call's stable/volatile split."""

    def __init__(self, replies: Sequence[str | Exception]) -> None:
        super().__init__(replies)
        self.parts: list[tuple[tuple[str, ...], str]] = []

    def complete_parts(self, stable: Sequence[str], volatile: str) -> str:
        self.parts.append((tuple(stable), volatile))
        return self.complete("".join(stable) + volatile)


def _planner(llm: object, **kwargs: object) -> Planner:
    options: dict[str, object] = {"strategy": "auto", "max_retries": 3}
    options.update(kwargs)
    planner = Planner(llm, **options)  # type: ignore[arg-type]
    planner._sleep = lambda _seconds: None  # type: ignore[method-assign]
    return planner


def _run(
    llm: object, view: InventoryView, task: str = TASK, **kwargs: object
) -> tuple[list[PlannerCall], PlanOutcome]:
    calls: list[PlannerCall] = []
    outcome = _planner(llm, **kwargs).plan(
        task, view.inventory, view=view, observer=calls.append
    )
    return calls, outcome


class TestStrategySelection:
    def test_a_repo_that_fits_is_planned_full(
        self, synthetic_10: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_10.store)
        stub = Stub([_plan(REAL_TARGET)])
        calls, outcome = _run(stub, view)
        assert outcome.strategy == "full"
        assert len(stub.prompts) == 1 and len(calls) == 1
        prompt = stub.prompts[0]
        for path in view.files:
            assert f"\n{path}  (" in prompt
        assert "REPOSITORY TREE" not in prompt
        assert '"expand"' not in prompt
        assert outcome.seen_files is None
        assert calls[0].inventory_files_shown == len(view.files)

    @pytest.mark.parametrize("repo", ["synthetic_100", "synthetic_1000"])
    def test_a_larger_repo_is_planned_by_retrieval_within_budget(
        self, repo: str, request: pytest.FixtureRequest
    ) -> None:
        view = view_of(request.getfixturevalue(repo).store)
        calls, outcome = _run(Stub([_plan(REAL_TARGET)]), view)
        assert outcome.strategy == "retrieval"
        assert calls[0].strategy == "retrieval"
        assert calls[0].inventory_chars / 4 <= 12_000
        assert calls[0].seed_files >= 1

    def test_oneshot_named_explicitly_is_byte_identical(self) -> None:
        inventory = [NodeId(x) for x in [
            "pkg/a.py::module_header::__header__", "pkg/a.py::function::load",
            "pkg/b.py::class::Store", "pkg/b.py::method::Store.get", "tools.py",
        ]]
        stub = Stub(["not json", _plan("pkg/a.py::function::load")])
        planner = _planner(
            stub, strategy="oneshot", agent_types=["anthropic_api", "ollama"],
            agent_labels=["anthropic_api — claude-sonnet-5 via Anthropic",
                          "ollama — qwen2.5-coder:14b via Ollama"],
        )
        planner.plan("Rename load to load_all and update callers.", inventory)
        golden = (GOLDEN / "oneshot_prompt.txt").read_text(encoding="utf-8")
        assert stub.prompts[0] == golden
        assert stub.prompts[1].startswith(golden + "\nYour previous response was")

    def test_oneshot_without_agents_is_byte_identical(self) -> None:
        stub = Stub([_plan("m.py")])
        _planner(stub, strategy="oneshot").plan("Do it.", [])
        golden = (GOLDEN / "oneshot_prompt_no_agents.txt").read_text(encoding="utf-8")
        assert stub.prompts == [golden]

    def test_an_unknown_strategy_is_refused(self) -> None:
        with pytest.raises(ValueError, match="strategy"):
            Planner(Stub([]), strategy="bogus")


class TestExpansionRounds:
    def test_two_expansions_then_a_plan(self, synthetic_100: SyntheticRepo) -> None:
        view = view_of(synthetic_100.store)
        stub = Stub([
            _expand("synth/p00/q00/m00.py"), _expand("synth/p01/q00/m00.py"),
            _plan(REAL_TARGET),
        ])
        calls, outcome = _run(stub, view)
        assert [c.outcome for c in calls] == ["expand", "expand", "plan"]
        assert [c.phase for c in calls] == ["plan", "expand", "expand"]
        assert [c.round for c in calls] == [0, 1, 2]
        assert outcome.summary.rounds == 3
        assert outcome.plan[0].target_nodes == [NodeId(REAL_TARGET)]
        assert "synth/p01/q00/m00.py  (12 nodes" in stub.prompts[2]

    def test_rounds_run_out(self, synthetic_100: SyntheticRepo) -> None:
        view = view_of(synthetic_100.store)
        stub = Stub([
            _expand("synth/p00/q00/m00.py"), _expand("synth/p00/q00/m01.py"),
            _expand("synth/p00/q00/m02.py"), _expand("synth/p00/q00/m03.py"),
            _plan(REAL_TARGET),
        ])
        calls, outcome = _run(stub, view, max_expansions=3)
        # The fourth prompt closes expansion; asking again is a rejected reply,
        # retried inside the same round — never a fifth round.
        assert stub.prompts[3].endswith(CLOSED_DIRECTIVE)
        assert CLOSED_ERROR in stub.prompts[4]
        assert max(c.round for c in calls) == 3
        assert [c.outcome for c in calls][-2:] == ["rejected", "plan"]
        assert calls[-1].attempt == 2

    def test_zero_expansions_plans_from_the_first_prompt(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        stub = Stub([_plan(REAL_TARGET)])
        _run(stub, view, max_expansions=0)
        assert stub.prompts[0].endswith(CLOSED_DIRECTIVE)
        assert '"expand"' not in stub.prompts[0]

    def test_unknown_shown_and_non_python_paths_are_answered_not_retried(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        stub = Stub([
            _expand(INVOICE_FILE, "synth/p00/q00/m0.py", "README.md"),
            _plan(REAL_TARGET),
        ])
        calls, _ = _run(stub, view)
        prompt = stub.prompts[1]
        assert f"already shown: {INVOICE_FILE}" in prompt
        assert "not found: synth/p00/q00/m0.py (close: synth/p00/q00/m0" in prompt
        assert "not a Python file: README.md" in prompt
        assert [(c.round, c.attempt) for c in calls] == [(0, 1), (1, 1)]

    def test_a_directory_opens_one_level_not_every_file(
        self, synthetic_1000: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_1000.store)
        tree = view.render_tree(3000)
        shut = next(
            line.strip().split(" ", 1)[0]
            for line in tree.text.splitlines() if line.endswith("expand to see]")
        )
        key = next(
            k for k in _dir_keys(view) if k.endswith(shut) and k not in tree.expanded
        )
        stub = Stub([_expand(key), _plan(REAL_TARGET)])
        calls, _ = _run(stub, view)
        expansion = stub.prompts[1].split("EXPANSION 1", 1)[1]
        assert f"\n{key} (" in expansion
        assert "::function::" not in expansion.split("\nReply with", 1)[0]
        assert calls[1].expanded_paths == 1

    def test_expansions_over_budget_are_listed_and_counted(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        wanted = [f"synth/p0{i}/q0{j}/m00.py" for i in range(3) for j in range(4)]
        stub = Stub([_expand(*wanted), _plan(REAL_TARGET)])
        calls, _ = _run(stub, view, inventory_token_budget=2_000)
        prompt = stub.prompts[1]
        assert "not expanded (inventory budget reached): " in prompt
        refused = prompt.split("not expanded (inventory budget reached): ", 1)[1]
        refused_paths = refused.split("\n", 1)[0].split("; ")
        shown = [p for p in wanted if f"\n{p}  (" in prompt]
        # In request order: every shown path precedes every refused one.
        assert shown and refused_paths
        assert wanted.index(shown[-1]) < wanted.index(refused_paths[0])
        assert calls[1].symbols_truncated >= len(refused_paths)
        for call in calls:
            assert call.inventory_chars / 4 <= 2_000

    def test_a_reply_with_both_expand_and_a_plan_is_retried(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        both = json.dumps({"expand": [INVOICE_FILE], "subtasks": []})
        stub = Stub([both, _plan(REAL_TARGET)])
        calls, _ = _run(stub, view)
        assert calls[0].outcome == "rejected"
        assert (calls[1].round, calls[1].attempt) == (0, 2)
        assert "not both" in stub.prompts[1]


def _dir_keys(view: InventoryView) -> list[str]:
    keys = set()
    for path in view.files:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            keys.add("/".join(parts[:depth]) + "/")
    return sorted(keys)


class TestVerification:
    UNSEEN = "synth/p00/q00/m00.py"

    def test_an_unseen_target_gets_one_verify_round(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        real = next(str(n) for n in view.inventory
                    if str(n).startswith(f"{self.UNSEEN}::function::"))
        stub = Stub([_plan(f"{self.UNSEEN}::function::made_up"), _plan(real)])
        calls, outcome = _run(stub, view)
        assert [c.phase for c in calls] == ["plan", "verify"]
        note = "VERIFY: Your plan targets ids in files you had not seen"
        assert note in stub.prompts[1]
        assert f"\n{self.UNSEEN}  (12 nodes" in stub.prompts[1]
        assert outcome.plan[0].target_nodes == [NodeId(real)]
        assert self.UNSEEN in outcome.seen_files

    def test_without_rounds_left_the_plan_is_returned_for_validation(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        stub = Stub([_plan(f"{self.UNSEEN}::function::made_up")])
        calls, outcome = _run(stub, view, max_expansions=0)
        assert len(calls) == 1
        assert self.UNSEEN not in outcome.seen_files

    def test_an_exact_existing_id_in_an_unseen_file_is_accepted(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        real = next(str(n) for n in view.inventory
                    if str(n).startswith(f"{self.UNSEEN}::function::"))
        calls, outcome = _run(Stub([_plan(real)]), view)
        assert len(calls) == 1
        assert self.UNSEEN not in outcome.seen_files

    def test_a_new_file_is_a_legitimate_target(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        calls, _ = _run(Stub([_plan("synth/new_module.py")]), view)
        assert len(calls) == 1


class TestPromptParts:
    def test_a_retry_changes_only_the_volatile_part(
        self, synthetic_10: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_10.store)
        stub = PartsStub(["{not json", _plan(REAL_TARGET)])
        _run(stub, view)
        (stable_1, volatile_1), (stable_2, volatile_2) = stub.parts
        assert stable_1 == stable_2
        assert volatile_1 == ""
        assert "Your previous response was rejected" in volatile_2

    def test_each_round_extends_the_previous_rounds_prefix(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        stub = PartsStub([
            _expand("synth/p00/q00/m00.py"), "{broken",
            _expand("synth/p00/"), _plan(REAL_TARGET),
        ])
        _run(stub, view)
        stables = [stable for stable, _ in stub.parts]
        for earlier, later in zip(stables, stables[1:], strict=False):
            assert later[: len(earlier)] == earlier
        assert "".join(stables[0]) == "".join(stables[1])[: len("".join(stables[0]))]

    def test_the_task_follows_the_inventory(self, synthetic_10: SyntheticRepo) -> None:
        # So the instructions and inventory form a prefix shared by every task.
        view = view_of(synthetic_10.store)
        stub = PartsStub([_plan(REAL_TARGET), _plan(REAL_TARGET)])
        _run(stub, view, task="first task")
        _run(stub, view, task="second task")
        assert stub.parts[0][0][0] == stub.parts[1][0][0]
        assert "first task" in stub.parts[0][0][1]

    @pytest.mark.parametrize("strategy", ["oneshot", "full", "retrieval", "auto"])
    def test_a_complete_only_llm_works_under_every_strategy(
        self, strategy: str, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        _calls, outcome = _run(Stub([_plan(REAL_TARGET)]), view, strategy=strategy)
        assert outcome.plan[0].task_id == "t"

    def test_a_complete_only_llm_works_under_outline(self) -> None:
        outline = json.dumps([{"step_id": "s", "description": "d", "files": ["a.py"]}])
        stub = Stub([outline, _plan("a.py::function::f")])
        calls: list[PlannerCall] = []
        outcome = _planner(stub, strategy="outline").plan(
            "t", [NodeId("a.py::function::f")], observer=calls.append
        )
        assert [c.phase for c in calls] == ["outline", "detail"]
        assert outcome.summary.rounds == 2
        assert outcome.plan[0].task_id == "s0.t"


class TestCallersParagraph:
    def test_auto_caller_tasks_tells_the_model_mak_adds_them(
        self, synthetic_10: SyntheticRepo
    ) -> None:
        stub = Stub([_plan(REAL_TARGET)])
        _run(stub, view_of(synthetic_10.store))
        assert "MAK then adds caller-update tasks" in stub.prompts[0]
        assert "CASCADE PREVENTION" not in stub.prompts[0]

    def test_without_it_the_model_must_add_every_caller(
        self, synthetic_10: SyntheticRepo
    ) -> None:
        stub = Stub([_plan(REAL_TARGET)])
        _run(stub, view_of(synthetic_10.store), auto_caller_tasks=False)
        assert "you MUST also add tasks that update EVERY caller" in stub.prompts[0]
        assert '"← refs" column' in stub.prompts[0]


class _Billed(Stub):
    """Reports usage per call, like a real backend; can raise a truncation."""

    def __init__(self, replies: Sequence[str | Exception]) -> None:
        super().__init__(replies)
        self.last_usage: dict[str, int] = {}

    def complete(self, prompt: str) -> str:
        self.last_usage = {"input_tokens": 100, "output_tokens": 10,
                           "cached_input_tokens": 40}
        return super().complete(prompt)


class TestTelemetry:
    def test_every_call_is_reported_even_when_planning_fails(
        self, synthetic_10: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_10.store)
        stub = _Billed([TruncatedResponseError("cut"), RuntimeError("down"), "{bad"])
        calls: list[PlannerCall] = []
        planner = _planner(stub)
        with pytest.raises(PlannerFailedError):
            planner.plan("secret task text", view.inventory, view=view,
                         observer=calls.append)
        assert [c.outcome for c in calls] == ["truncated", "call_failed", "rejected"]
        assert [c.attempt for c in calls] == [1, 2, 3]
        # The truncated and rejected replies were generated and billed.
        assert planner.token_usage["input_tokens"] == 300
        for call in calls:
            assert "secret task text" not in json.dumps(asdict(call))

    def test_usage_and_summary(self, synthetic_10: SyntheticRepo) -> None:
        view = view_of(synthetic_10.store)
        calls, outcome = _run(_Billed(["{bad", _plan(REAL_TARGET)]), view)
        assert [c.input_tokens for c in calls] == [100, 100]
        assert [c.cached_input_tokens for c in calls] == [40, 40]
        summary = outcome.summary
        assert (summary.calls, summary.rounds) == (2, 1)
        assert (summary.input_tokens, summary.cached_tokens) == (200, 80)
        assert summary.output_tokens == 20
        assert calls[0].stable_chars < calls[0].prompt_chars or (
            calls[0].stable_chars == calls[0].prompt_chars
        )

    def test_decompose_keeps_its_signature(self, synthetic_10: SyntheticRepo) -> None:
        view = view_of(synthetic_10.store)
        tasks = _planner(Stub([_plan(REAL_TARGET)])).decompose(TASK, view.inventory)
        assert [t.task_id for t in tasks] == ["t"]


class TestParseReply:
    def test_plan_and_expand_shapes(self) -> None:
        assert parse_reply(_plan("a.py")).plan[0].task_id == "t"  # type: ignore[union-attr]
        request = parse_reply(_expand("a.py", "a.py", " b/ "))
        assert request.paths == ("a.py", "b/")  # type: ignore[union-attr]

    @pytest.mark.parametrize("raw", [
        '{"expand": []}', '{"expand": "a.py"}', '{"expand": [1]}',
        '{"expand": ["a.py"], "why": 3}', '{"expand": ["a.py"], "task_id": "x"}',
    ])
    def test_malformed_expand_requests_are_value_errors(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_reply(raw)


class TestOllamaRounds:
    def test_every_round_of_a_plan_requests_the_same_window(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        from typing import Any

        from mak.local.ollama_client import OllamaChatResponse, OllamaModel
        from mak.planner.llm import OllamaPlannerLLM

        class Scripted:
            def __init__(self, replies: list[str]) -> None:
                self.replies = replies
                self.calls: list[dict[str, Any]] = []

            def show(self, model: str) -> OllamaModel:
                return OllamaModel(name=model, context_length=65536)

            def chat(self, **kwargs: Any) -> OllamaChatResponse:
                self.calls.append(kwargs)
                return OllamaChatResponse(content=self.replies.pop(0))

        client = Scripted([
            _expand("synth/p00/q00/m00.py"), _expand("synth/p01/"), _plan(REAL_TARGET),
        ])
        llm = OllamaPlannerLLM(
            model="qwen", client=client,  # type: ignore[arg-type]
            max_tokens=4096, prompt_budget_tokens=12_000,
        )
        calls, _ = _run(llm, view_of(synthetic_100.store))
        assert len(calls) == len(client.calls) == 3
        assert len({c["options"]["num_ctx"] for c in client.calls}) == 1


class TestNothingSilent:
    """Whatever the budget leaves out is in the prompt and in PLANNER_CALL."""

    def test_collapsed_directories(self, synthetic_1000: SyntheticRepo) -> None:
        stub = Stub([_plan(REAL_TARGET)])
        calls, _ = _run(stub, view_of(synthetic_1000.store))
        assert calls[0].collapsed_dirs > 0
        assert f"{calls[0].collapsed_dirs:,} directories collapsed" in stub.prompts[0]
        assert "[collapsed — expand to see]" in stub.prompts[0]

    def test_a_file_larger_than_a_quarter_of_the_budget(self) -> None:
        big = [NodeId(f"big.py::function::f{i:03d}") for i in range(400)]
        small = [NodeId(f"pkg/m{i}.py::function::g") for i in range(40)]
        view = InventoryView([*big, *small])
        stub = Stub([_expand("big.py"), _plan("big.py::function::f000")])
        calls, _ = _run(stub, view, inventory_token_budget=2_000)
        assert calls[0].strategy == "retrieval"
        assert "more nodes not shown (budget); their ids are still valid targets" in (
            stub.prompts[1]
        )
        assert calls[1].symbols_truncated > 0
        assert calls[1].inventory_chars / 4 <= 2_000
