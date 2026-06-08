"""ConflictDetector: orchestrate the shallow cross-node validation checks.

The detector runs between an agent's output being committed to the node store and
that output being accepted. It composes the three structural checks — signature
compatibility, import consistency, and name collision — plus a parse gate, and
returns a single pass/fail ``ConflictReport`` with human-readable reasons. It is
intentionally shallow: it gates on ``ast.parse`` success and the structural checks
only; full correctness is the test suite's responsibility.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from mak.conflict_detector.import_check import check_import_conflicts
from mak.conflict_detector.name_collision_check import check_name_collisions
from mak.conflict_detector.signature_check import check_signature_compatibility


@dataclass(frozen=True, slots=True)
class Conflict:
    """One detected problem, tagged with the check that found it."""

    check: str  # "syntax" | "signature" | "import" | "name_collision"
    message: str


@dataclass(frozen=True, slots=True)
class ConflictReport:
    """The aggregate result of running the detector over a round of edits."""

    conflicts: tuple[Conflict, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no conflicts were found."""
        return not self.conflicts

    @property
    def reasons(self) -> list[str]:
        """All conflict messages, in detection order."""
        return [c.message for c in self.conflicts]

    def by_check(self, check: str) -> list[Conflict]:
        """Conflicts found by a specific check."""
        return [c for c in self.conflicts if c.check == check]


@dataclass
class EditRound:
    """The inputs for one round of conflict detection.

    All fields are optional so a caller can run only the checks relevant to what
    actually changed in a round.

    - ``definitions``: node_id -> new source that *defines* functions/methods,
      used as the authority for signature checks.
    - ``callers``: node_id -> source that *calls* functions, checked against the
      definitions above.
    - ``header_edits``: agent_id -> ``__header__`` source, for import consistency.
    - ``symbol_edits``: agent_id -> introduced source, for name-collision checks.
    """

    definitions: dict[str, str] = field(default_factory=dict)
    callers: dict[str, str] = field(default_factory=dict)
    header_edits: dict[str, str] = field(default_factory=dict)
    symbol_edits: dict[str, str] = field(default_factory=dict)


class ConflictDetector:
    """Runs the structural cross-node checks and reports pass/fail with reasons."""

    def detect(self, edits: EditRound) -> ConflictReport:
        """Run every applicable check over ``edits`` and aggregate the result."""
        conflicts: list[Conflict] = []
        conflicts.extend(self._check_syntax(edits))
        # A parse failure makes the structural checks meaningless — gate on it.
        if not any(c.check == "syntax" for c in conflicts):
            conflicts.extend(self._check_signatures(edits))
            conflicts.extend(self._check_imports(edits))
            conflicts.extend(self._check_name_collisions(edits))
        return ConflictReport(tuple(conflicts))

    @staticmethod
    def _all_sources(edits: EditRound) -> dict[str, str]:
        return {
            **{f"definition:{k}": v for k, v in edits.definitions.items()},
            **{f"caller:{k}": v for k, v in edits.callers.items()},
            **{f"header:{k}": v for k, v in edits.header_edits.items()},
            **{f"symbol:{k}": v for k, v in edits.symbol_edits.items()},
        }

    def _check_syntax(self, edits: EditRound) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for label, source in self._all_sources(edits).items():
            try:
                ast.parse(source)
            except SyntaxError as exc:
                conflicts.append(
                    Conflict("syntax", f"{label} failed to parse: {exc.msg}")
                )
        return conflicts

    def _check_signatures(self, edits: EditRound) -> list[Conflict]:
        conflicts: list[Conflict] = []
        # Every defining source is an authority; check every caller against all of
        # them. This catches a caller that targets a function defined in any node.
        merged_definitions = "\n\n".join(edits.definitions.values())
        if not merged_definitions.strip():
            return conflicts
        for caller_id, caller_source in edits.callers.items():
            for reason in check_signature_compatibility(
                merged_definitions, caller_source
            ):
                conflicts.append(Conflict("signature", f"{caller_id}: {reason}"))
        return conflicts

    def _check_imports(self, edits: EditRound) -> list[Conflict]:
        return [
            Conflict("import", reason)
            for reason in check_import_conflicts(edits.header_edits)
        ]

    def _check_name_collisions(self, edits: EditRound) -> list[Conflict]:
        return [
            Conflict("name_collision", reason)
            for reason in check_name_collisions(edits.symbol_edits)
        ]
