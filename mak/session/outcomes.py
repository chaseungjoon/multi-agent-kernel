"""What an attempt's outcome means: failure record, retry, or asserted no-op."""

from __future__ import annotations

from collections.abc import Callable

from mak.agent_runner.adapters.budget import TRUNCATION_STOP_REASONS
from mak.agent_runner.registry import AdapterRegistry
from mak.agent_runner.stop_signals import matches
from mak.core.exceptions import (
    SchedulingError,
    SessionError,
    UnsafeNodeIdError,
)
from mak.core.logging import EventType
from mak.core.types import NodeId, TaskBundle, TaskResult
from mak.node_store.reconstruction import reconstruct_file
from mak.semantic.sources import StoreSources
from mak.session.commit.apply import CommitApplier
from mak.session.concurrency import ConcurrentRunner
from mak.session.events import EventLog
from mak.session.failures import final_failure_reason
from mak.session.grants import release_lock
from mak.session.repair import unresolved_obligations
from mak.session.store_view import StoreView, file_of
from mak.session.types import LockTableLike, SubTaskProgress
from mak.session.wave import WaveState
from mak.session.workspace import Workspace


class RetryPolicy:
    """Close, retry, or fail a task once its attempt has been settled."""

    def __init__(
        self, *, registry: AdapterRegistry, max_attempts: int, log: EventLog
    ) -> None:
        self._registry = registry
        self.max_attempts = max_attempts
        self._log = log

    def finish_task(self, wave: WaveState, task_id: str) -> None:
        """Mark a task whose every grant committed as complete."""
        wave.require_scheduler().on_task_complete(task_id)
        wave.completed.append(task_id)
        self._log(EventType.TASK_COMPLETED, task_id=task_id)

    def handle_incomplete(
        self,
        wave: WaveState,
        progress: SubTaskProgress,
        result: TaskResult | None = None,
    ) -> None:
        """Retry remaining grants, or fail the task once attempts are exhausted.

        A retry is only worth an attempt if it can differ from the one that
        failed. Two cases where it cannot:

        - the provider *refused* — the same prompt earns the same refusal, so the
          task fails now rather than after three identical calls;
        - nothing at all was learned — impossible here, since every path that
          reaches this point has recorded a failure reason, which is fed back to
          the agent as ``retry_note`` on the re-dispatch.
        """
        scheduler = wave.require_scheduler()
        exhausted = progress.attempts >= self.max_attempts
        unretryable = result is not None and not result.retryable
        if exhausted or unretryable:
            scheduler.on_task_failed(progress.task_id, requeue=False)
            self._fail(wave, progress, exhausted=exhausted, unretryable=unretryable)
            return
        # Remaining nodes are still locked from the original acquisition; queue
        # a narrowed re-dispatch covering only what is left, carrying why the
        # last attempt produced nothing.
        progress.retry_note = self._retry_note(wave, progress, result)
        wave.redispatches += 1
        wave.partial_queue.append(progress.task_id)

    def _fail(
        self,
        wave: WaveState,
        progress: SubTaskProgress,
        *,
        exhausted: bool,
        unretryable: bool,
    ) -> None:
        """Record a failed task and the reason it will be reported with."""
        wave.failed.append(progress.task_id)
        reason = final_failure_reason(wave, progress)
        wave.failure_reasons[progress.task_id] = reason
        if unretryable and not exhausted:
            reason = (
                f"{reason} (not retryable — the remaining "
                f"{self.max_attempts - progress.attempts} attempt(s) would "
                "repeat it verbatim)"
            )
            wave.failure_reasons[progress.task_id] = reason
        self._log(
            EventType.TASK_FAILED,
            task_id=progress.task_id,
            failed=True,
            reason=reason,
        )

    @staticmethod
    def _retry_note(
        wave: WaveState, progress: SubTaskProgress, result: TaskResult | None
    ) -> str | None:
        """Return the instruction to attach to the next attempt at this task.

        *What* to change depends on how the attempt failed:

        - a **kernel note** (a stale read, a broken promise) outranks everything:
          it is the one thing the next attempt must act on;
        - a **truncation** gets a compaction instruction rather than the generic
          "that failed, try again": re-sending an identical request produces an
          identically-cut reply;
        - a **schema slip** gets the schema restated, because the generic note
          says the previous answer was unusable but never what shape was wanted.
        """
        if progress.kernel_note is not None:
            note, progress.kernel_note = progress.kernel_note, None
            return note
        reason = wave.failure_reasons.get(progress.task_id)
        truncated = result is not None and matches(
            result.stop_reason, TRUNCATION_STOP_REASONS
        )
        if not truncated and result is not None and result.error_kind == "protocol":
            return (
                f"Your previous response did not match the result schema: {reason}. "
                "'modified_fragments' must be a JSON array of objects, each "
                '{"node_id": "<an id copied verbatim from target_nodes>", '
                '"new_source": "<the node\'s complete new source>"}. Emit it as '
                "structured tool input — not as a string, and not as a "
                "JSON-encoded array inside a string. If you have nothing to "
                "return, set no_changes_required instead of sending an empty or "
                "differently-shaped field."
            )
        if truncated:
            return (
                "Your previous response was cut off at the model's output-token "
                "limit before the result was complete, so none of it could be "
                "used. Return the same work in less output: emit only the nodes "
                "you actually changed, no commentary, and no unchanged code. If "
                "one node's full source genuinely cannot fit in a single "
                "response, return success=false with an error saying so rather "
                "than a partial rewrite."
            )
        if reason is None:
            return None
        return (
            f"Your previous attempt at this task produced nothing usable: {reason}. "
            "Do not repeat it — return the full source of every node you change, "
            "under the exact node ids in target_nodes."
        )

    def submit_partials(
        self, wave: WaveState, runner_factory: Callable[[], ConcurrentRunner]
    ) -> None:
        """Re-dispatch the narrowed remaining grants of each partial task (async)."""
        if not wave.partial_queue:
            return
        queued, wave.partial_queue = wave.partial_queue, []
        runner = runner_factory()
        for task_id in queued:
            progress = wave.progress[task_id]
            task = wave.task(task_id)
            adapter = self._registry.get(task.agent_type)
            bundle = TaskBundle(
                task_id=task_id,
                description=task.description,
                target_nodes=progress.remaining,
                retry_note=progress.retry_note,
            )
            runner.assign(adapter, bundle)
        # A re-dispatch replaced those tasks' read sets; persist them now rather
        # than at the next unrelated state transition.
        wave.require_scheduler().save()


class NoopPolicy:
    """Decide whether an agent's "nothing needed changing" closes its grants."""

    def __init__(
        self,
        *,
        view: StoreView,
        workspace: Workspace,
        lock_table: LockTableLike,
        applier: CommitApplier,
        log: EventLog,
    ) -> None:
        self._view = view
        self._workspace = workspace
        self._lock_table = lock_table
        self._applier = applier
        self._log = log

    @staticmethod
    def is_asserted(result: TaskResult) -> bool:
        """Whether the agent *claimed* there was nothing to change.

        "Success with no fragments" is not enough: a reply cut off at the
        provider's output cap satisfies it exactly. Acceptance needs positive
        evidence the agent could only have produced by finishing — the flag it
        was asked to set — and a reply that carries no work.
        """
        return (
            result.success
            and result.no_changes_required
            and not result.modified_nodes
            and not result.new_sources
        )

    def accept(
        self, wave: WaveState, progress: SubTaskProgress, result: TaskResult
    ) -> list[str]:
        """Close the grants of a task the agent asserted needed no change.

        Returns the reasons any grant was **refused**, for the caller to record.

        Gated on the target existing and its file parsing — an assertion that
        nothing needs changing is not evidence about a file that is missing, or
        one whose syntax error is the very bug the task was sent to fix — and on
        the target having been there to inspect at all (:meth:`_refusal`).
        """
        refusals: list[str] = []
        obligation_refusal = self._repair_refusal(wave, progress.task_id)
        if obligation_refusal is not None:
            return [obligation_refusal]
        for node_id in progress.target_nodes:
            if node_id in progress.completed_nodes:
                continue
            refusal = self._refusal(wave, progress, node_id)
            if refusal is not None:
                refusals.append(refusal)
                continue
            if not self.target_exists(node_id) or not self._view.file_compiles(
                node_id
            ):
                continue
            # Sync committed node store content to disk. The on-disk file may
            # pre-date the committed version (e.g. an earlier MAK run wrote a
            # corrected whole-file node but failed to reconstruct because other
            # fragments were still broken).
            try:
                self._applier.reconstruct_affected([node_id])
            except (SyntaxError, OSError):
                pass
            progress.completed_nodes.add(node_id)
            release_lock(self._lock_table, wave, progress.task_id, node_id)
            progress.noop_nodes.add(node_id)
        if progress.noop_nodes:
            self._log(
                EventType.ACCEPTED_NOOP,
                task_id=progress.task_id,
                attempt=progress.attempts,
                nodes=[str(n) for n in sorted(progress.noop_nodes)],
                reason=result.error or "agent asserted no changes were required",
            )
        return refusals

    def _repair_refusal(self, wave: WaveState, task_id: str) -> str | None:
        """Refuse a no-op while a kernel-owned repair postcondition is false."""
        task = wave.task(task_id)
        if not task.repair_obligations:
            return None
        scope = frozenset(
            {
                path
                for obligation in task.repair_obligations
                for path in (obligation.file, obligation.defining_file)
            }
        )
        unresolved = [
            obligation.detail
            for obligation in unresolved_obligations(
                StoreSources(self._view.store), task, scope
            )
        ]
        if not unresolved:
            return None
        return (
            "no_changes_required cannot discharge an unresolved repair "
            f"obligation: {'; '.join(unresolved)}"
        )

    def _refusal(
        self, wave: WaveState, progress: SubTaskProgress, node_id: NodeId
    ) -> str | None:
        """Why this grant may not be closed by assertion, or None if it may.

        ``no_changes_required`` is the one completion an agent awards itself, so
        it needs a *work* check, not only an existence check. Two cases where the
        assertion cannot be true whatever the agent believes, both read off this
        wave's own plan rather than off the agent's answer:

        - a **dependency created the target**. MAK's own ``depends_on`` edge says
          the file did not exist until an earlier task in this wave wrote it, so
          "I looked and nothing needed changing" describes an inspection that
          could not have happened when the plan was written.
        - a **greenfield whole-file grant on the first attempt**. Same reasoning
          without the edge: the wave itself is what created the file. Only the
          first attempt is refused — a second attempt has seen the retry note and
          the file's real contents, so its assertion is about something.

        Everything else — a target that predates the wave, a later attempt — is
        accepted.
        """
        file_path = file_of(str(node_id))
        if file_path in wave.preexisting_files:
            return None
        creator = _dependency_creating(wave, progress.task_id, file_path)
        if creator is not None:
            return (
                f"'{node_id}' did not exist when this wave was planned — task "
                f"'{creator}', which this task depends on, is what created it. "
                "'no changes required' cannot describe code you inspected before "
                "it existed: read the file as it stands now and make the change "
                "this task asks for."
            )
        if "::" not in str(node_id) and progress.attempts <= 1:
            return (
                f"'{node_id}' did not exist when this wave was planned, so there "
                "was nothing to inspect; a first-attempt 'no changes required' on "
                "a whole file this wave itself creates is not an assessment. "
                "Return the file's complete source."
            )
        return None

    def describe_empty_result(
        self,
        progress: SubTaskProgress,
        result: TaskResult,
        noop_refusals: list[str] | None = None,
    ) -> str:
        """Explain why a *successful* agent result left nothing to commit.

        A symptom shared by several distinct causes is named by its cause, so a
        failed run can be diagnosed without re-running it. A refused no-op is
        reported first and verbatim: it is the most specific answer there is, and
        it is phrased as the instruction the retry needs.
        """
        if noop_refusals:
            return "; ".join(noop_refusals)
        granted = ", ".join(str(n) for n in progress.target_nodes)
        reported = dict.fromkeys([*result.modified_nodes, *result.new_sources])
        returned = [str(n) for n in reported]
        if result.stop_reason is not None and matches(
            result.stop_reason, TRUNCATION_STOP_REASONS
        ):
            return (
                "the agent's reply was cut off at the model's output-token limit "
                f"(stop reason: {result.stop_reason}), so no complete source "
                f"arrived (granted: {granted})"
            )
        if returned and result.new_sources:
            return (
                f"agent returned {len(returned)} node id(s), none within its grant "
                f"(granted: {granted}; returned: {', '.join(returned)})"
            )
        if returned:
            return (
                f"agent listed {len(returned)} modified node(s) but returned no "
                f"source for any of them (returned: {', '.join(returned)})"
            )
        return self._describe_unchanged_target(progress, granted)

    def _describe_unchanged_target(
        self, progress: SubTaskProgress, granted: str
    ) -> str:
        """Explain an empty reply that named no node at all."""
        missing = [n for n in progress.remaining if not self.target_exists(n)]
        if missing:
            return (
                "agent returned success with no sources and the target does not "
                f"exist (missing: {', '.join(str(n) for n in missing)})"
            )
        invalid = [n for n in progress.remaining if not self._view.file_compiles(n)]
        if invalid:
            return (
                "agent returned success with no changes, but the target file is "
                f"still not valid Python ({', '.join(str(n) for n in invalid)})"
            )
        return (
            "agent returned success with no sources and did not assert that no "
            f"change was required (granted: {granted}); an empty reply is not "
            "evidence the task was done — it is also what a reply cut off at the "
            "output-token limit looks like"
        )

    def target_exists(self, node_id: NodeId) -> bool:
        """Whether a target already exists committed (so a no-op leaves it intact).

        True if the node itself is committed, or — for a whole-file target (a bare
        ``path.py``) — if the file already has committed fragments from ingestion.
        In both cases the file must also exist on disk. If the node is committed but
        the file has been deleted (stale node store from a prior session), it is
        reconstructed from committed fragments before returning True.
        """
        file_path = file_of(str(node_id))
        store = self._view.store
        in_store = self._view.source(node_id) is not None or (
            "::" not in str(node_id)
            and bool(store.get_committed_fragments(str(node_id)))
        )
        if not in_store:
            return False
        if (self._workspace.work_dir / file_path).exists():
            return True
        # Node is committed but file is missing from disk — reconstruct it so
        # the no-op acceptance does not silently leave the filesystem inconsistent.
        fragments = store.get_committed_fragments(file_path)
        if not fragments:
            return False
        try:
            reconstruct_file(
                fragments, output_path=self._workspace.safe_output_path(file_path)
            )
            return True
        except (SyntaxError, OSError, UnsafeNodeIdError):
            return False


def _dependency_creating(wave: WaveState, task_id: str, file_path: str) -> str | None:
    """Return the depended-on task that targets ``file_path``, if any.

    Direct edges only. A transitive ancestor's output has been visible to
    everything downstream of it for at least one commit, so the "nothing
    existed to inspect" argument does not hold there.
    """
    try:
        task = wave.task(task_id)
    except (SessionError, SchedulingError, KeyError):
        return None
    for dep_id in task.depends_on:
        try:
            dep = wave.task(dep_id)
        except (SessionError, SchedulingError, KeyError):
            continue
        if any(file_of(str(target)) == file_path for target in dep.target_nodes):
            return dep_id
    return None
