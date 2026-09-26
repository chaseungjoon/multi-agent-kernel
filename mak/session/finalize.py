"""Teardown: run the project's suite and decide whether the run may be pushed."""

from __future__ import annotations

from mak.config import MakConfig
from mak.core.logging import EventType
from mak.execution_result import ExecutionResult
from mak.git_integration.git import GitHelper
from mak.session.events import EventLog
from mak.session.types import SessionResult, SessionState, TestRunner
from mak.session.wave import WaveState
from mak.teardown import SuiteOutcome, TeardownResult, may_push


class Finalizer:
    """Runs the configured test suite and applies the push gate."""

    def __init__(
        self,
        *,
        config: MakConfig,
        git: GitHelper | None,
        test_runner: TestRunner | None,
        log: EventLog,
    ) -> None:
        self._config = config
        self._git = git
        self._test_runner = test_runner
        self._log = log

    def teardown(
        self,
        wave: WaveState,
        execution: ExecutionResult | None,
        last_result: SessionResult | None,
    ) -> TeardownResult:
        """Run the test suite and decide, honestly, whether to push.

        Two gates. The suite's outcome is one of four
        (:class:`~mak.teardown.SuiteOutcome`): no configured suite is
        ``SKIPPED``, never a pass, and a runner that raises is an ``ERROR``.
        And the push additionally requires the **aggregate** execution outcome to
        be satisfied — ``execution`` when the caller has one (it knows about
        cascade waves; the session does not), else the session's last result —
        so failed, blocked or skipped work is never pushed.
        """
        outcome, output = self._run_tests()
        aggregate = execution or ExecutionResult(
            initial=last_result or _empty_result()
        )
        pushed, skip_reason = self._maybe_push(outcome, aggregate)
        result = TeardownResult(
            outcome=outcome,
            output=output,
            pushed=pushed,
            push_skipped_reason=skip_reason,
        )
        self._log(
            EventType.SESSION_ENDED,
            test_outcome=str(outcome),
            tests_passed=outcome is SuiteOutcome.PASSED,
            pushed=pushed,
            push_skipped=skip_reason,
            completed=len(wave.completed),
            failed=len(wave.failed),
            output=output[:500],
        )
        return result

    def _run_tests(self) -> tuple[SuiteOutcome, str]:
        """Run the configured suite, if there is one, and classify the result."""
        if self._test_runner is None:
            return SuiteOutcome.SKIPPED, "no test_command configured; nothing ran"
        try:
            passed, output = self._test_runner()
        except Exception as exc:  # noqa: BLE001 - any runner defect is an outcome
            # Deliberately not re-raised: teardown's job is to report, and a
            # runner that blew up is a report, not a reason to lose the run's
            # results. It is an ERROR, never a pass.
            self._log(EventType.SESSION_ENDED, test_runner_error=str(exc))
            return SuiteOutcome.ERROR, f"the test runner raised: {exc}"
        return (SuiteOutcome.PASSED if passed else SuiteOutcome.FAILED), output

    def _maybe_push(
        self, outcome: SuiteOutcome, execution: ExecutionResult
    ) -> tuple[bool, str | None]:
        """Apply the push gate; return ``(pushed, why_not)``."""
        if not self._config.git.auto_push or self._git is None:
            return False, None
        if not execution.request_satisfied:
            return False, (
                "the run did not fully succeed "
                f"({execution.summary_line()}); nothing was pushed"
            )
        if not may_push(outcome, self._config.session.test_policy):
            return False, (
                f"tests {outcome.value} and session.test_policy is "
                f"'{self._config.session.test_policy}'; nothing was pushed"
            )
        self._git.push()
        return True, None


def _empty_result() -> SessionResult:
    """Stand in for a session that never ran, so teardown has a verdict."""
    return SessionResult(state=SessionState.CREATED, completed=(), failed=())
