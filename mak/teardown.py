"""Test outcomes and the push gate — what teardown actually knows.

``teardown`` began with ``passed = True`` and only ever moved it if a test runner
existed. With none configured — the default — it therefore logged
``tests_passed=True`` and, with ``auto_push`` on, pushed. "No tests ran" was
being reported, and acted on, as "the tests passed". It also never looked at the
run itself, so a wave with failed, blocked, or skipped tasks pushed just as
readily; and in the TUI a teardown that *raised* was shown as a warning while the
pass flag stayed ``True``.

A boolean cannot carry this. There are four outcomes, and the two that a boolean
collapses into "passed" are exactly the two that must not push:

* :attr:`SuiteOutcome.PASSED` — a suite ran and was green.
* :attr:`SuiteOutcome.FAILED` — a suite ran and was not.
* :attr:`SuiteOutcome.SKIPPED` — no ``test_command`` is configured. Nothing ran,
  so nothing is known.
* :attr:`SuiteOutcome.ERROR` — the runner itself blew up. Also nothing is known,
  and something is wrong.

The push gate then takes all of it: ``auto_push`` on, a git helper present, the
**aggregate** execution outcome satisfied, and the test policy met.
:class:`TeardownResult` records which of those refused, because "it did not push"
without a reason is not something an operator can act on.
"""

from __future__ import annotations

import dataclasses
from enum import StrEnum

# Policies for whether a run with no test suite may push. Named rather than a
# bare bool so the config value reads as the decision it is.
POLICY_REQUIRE_PASS = "require_pass"
POLICY_ALLOW_SKIP = "allow_skip"


class SuiteOutcome(StrEnum):
    """What teardown learned about the test suite.

    Named for the *suite*: ``TestOutcome`` is a name pytest tries to collect
    as a test class in every module that imports it.
    """

    PASSED = "passed"
    FAILED = "failed"
    # No test command configured: nothing ran, so nothing is known. Distinct from
    # PASSED on purpose — conflating them is what let a project with no suite
    # push on every run.
    SKIPPED = "skipped"
    # The runner raised. Also "nothing is known", but with a defect attached.
    ERROR = "error"

    @property
    def ran(self) -> bool:
        """Whether a suite actually executed."""
        return self in (SuiteOutcome.PASSED, SuiteOutcome.FAILED)


@dataclasses.dataclass(frozen=True, slots=True)
class TeardownResult:
    """The outcome of ``Session.teardown``."""

    outcome: SuiteOutcome
    output: str = ""
    pushed: bool = False
    # Why the push did not happen, when it did not. ``None`` when it did, or when
    # pushing was never asked for.
    push_skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        """Whether teardown found nothing wrong.

        A skipped suite is *not* a failure — a project may legitimately have no
        tests — so it does not make teardown "not ok". What a skip does do is
        fail to open the push gate under the default policy, which is a separate
        question and answered by :func:`may_push`.
        """
        return self.outcome in (SuiteOutcome.PASSED, SuiteOutcome.SKIPPED)


def may_push(outcome: SuiteOutcome, policy: str) -> bool:
    """Whether ``outcome`` satisfies the project's test policy for pushing.

    ``require_pass`` (the default) means what it says: only a suite that ran and
    passed opens the gate. ``allow_skip`` is the deliberate opt-out for a project
    with no suite that wants ``auto_push`` anyway — it accepts a skip, and still
    refuses a failure or an error, because those are not the absence of
    information, they are bad information.
    """
    if outcome is SuiteOutcome.PASSED:
        return True
    if outcome is SuiteOutcome.SKIPPED:
        return policy == POLICY_ALLOW_SKIP
    return False
