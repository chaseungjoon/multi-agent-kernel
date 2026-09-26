"""WaveState: every per-wave field is fresh for the next wave, by construction."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from mak.session.wave import WaveState
from tests.semantic.helpers import ScriptRunner, make_session, task

# Fields ``install_plan`` fills in for the new wave from its own plan, rather
# than leaving at their empty defaults.
_PLANNED_FIELDS = frozenset(
    {"scheduler", "graph", "lock_policy", "preexisting_files", "plan_findings",
     "progress"}
)


_TWO_FUNCS = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).write_text(text)


class TestStart:
    def test_start_twice_gives_equal_independent_objects(self) -> None:
        first, second = WaveState.start(), WaveState.start()
        assert first == second
        assert first is not second
        for field in dataclasses.fields(WaveState):
            value = getattr(first, field.name)
            if isinstance(value, list | dict | set):
                assert value is not getattr(second, field.name), field.name

    def test_mutating_one_wave_leaves_the_other_untouched(self) -> None:
        first, second = WaveState.start(), WaveState.start()
        first.completed.append("a")
        first.failure_history.setdefault("a", []).append("boom")
        first.waiting_on_providers.add("a")
        first.stale_reads += 1
        assert second == WaveState.start()

    def test_start_copies_what_it_is_given(self) -> None:
        files = {"m.py"}
        wave = WaveState.start(preexisting_files=files)
        files.add("n.py")
        assert wave.preexisting_files == {"m.py"}


class TestSecondWave:
    def test_nothing_from_a_failed_first_wave_is_visible_in_the_second(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {"m.py": _TWO_FUNCS})
        runner = ScriptRunner({
            "ok": [{"m.py::function::f": "def f():\n    return 10\n"}],
            # "bad" returns invalid Python, so its commit is rejected and it fails.
            "bad": [{"m.py::function::g": "def g(:\n"}],
            "next": [{"m.py::function::g": "def g():\n    return 20\n"}],
        })
        session, _, _ = make_session(tmp_path, runner, max_attempts=1)
        session.initialize()
        session.install_plan([
            task("ok", ["m.py::function::f"]),
            task("bad", ["m.py::function::g"]),
        ])
        first_result = session.run()
        first = session.wave
        # The first wave accumulated real state, so the assertion below means
        # something: a commit, a failure, and its history.
        assert first_result.failed == ("bad",)
        assert first.completed == ["ok"] and first.failed == ["bad"]
        assert first.committed and first.file_before and first.failure_history

        session.install_plan([task("next", ["m.py::function::g"])])
        second = session.wave
        assert second is not first
        fresh = WaveState.start()
        for field in dataclasses.fields(WaveState):
            if field.name in _PLANNED_FIELDS:
                continue
            assert getattr(second, field.name) == getattr(fresh, field.name), (
                field.name
            )
        assert set(second.progress) == {"next"}

        result = session.run()
        assert result.ok
        assert result.completed == ("next",)
        assert result.failed == () and result.failure_reasons == {}
