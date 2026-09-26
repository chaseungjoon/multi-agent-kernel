"""Collect finished agent calls in batches and settle each one."""

from __future__ import annotations

import queue
from collections import Counter

from mak.agent_runner.protocol import map_returned_sources
from mak.core.logging import EventType
from mak.core.types import NodeFragment, NodeId, TaskBundle, TaskResult
from mak.scheduler.scheduler import Scheduler
from mak.session.commit.pipeline import CommitPipeline
from mak.session.concurrency import Completion
from mak.session.events import EventLog
from mak.session.failures import record_failure
from mak.session.grants import release_lock
from mak.session.outcomes import NoopPolicy, RetryPolicy
from mak.session.parking import ParkedCommits, Settle
from mak.session.store_view import StoreView
from mak.session.types import LockTableLike, SubTaskProgress
from mak.session.wave import WaveState


class BatchProcessor:
    """Turns completions into commits, retries and failures, in a fixed order.

    Owns the completion queue the thread pool feeds, and the running total of
    agent token usage read off each result.
    """

    def __init__(
        self,
        *,
        view: StoreView,
        lock_table: LockTableLike,
        pipeline: CommitPipeline,
        parking: ParkedCommits,
        noop: NoopPolicy,
        retry: RetryPolicy,
        collect_timeout_s: float,
        log: EventLog,
    ) -> None:
        self._view = view
        self._lock_table = lock_table
        self._pipeline = pipeline
        self._parking = parking
        self._noop = noop
        self._retry = retry
        self.collect_timeout_s = collect_timeout_s
        self._log = log
        self.completions: queue.Queue[Completion] = queue.Queue()
        # Agent tokens spent, summed from what each provider reported on its own
        # response. Read off ``TaskResult.usage`` rather than by patching the SDK:
        # a counter hooked into one SDK entry point misses every call made through
        # another (``messages.stream`` versus ``messages.create``), and patching a
        # vendor's internals breaks with the vendor's next refactor.
        self.agent_usage: Counter[str] = Counter()

    def collect(self) -> list[Completion]:
        """Block for the first completion, then drain every result already done.

        Batching is what lets the conflict detector see *cross-agent* edits: all
        results that finished around the same time are validated together.
        Returns ``[]`` when nothing arrives within the collect timeout.
        """
        try:
            first = self.completions.get(timeout=self.collect_timeout_s)
        except queue.Empty:
            return []
        batch = [first]
        while True:
            try:
                batch.append(self.completions.get_nowait())
            except queue.Empty:
                break
        return batch

    def process_batch(self, wave: WaveState, batch: list[Completion]) -> None:
        """Validate and commit a batch of results in a deterministic order.

        Tasks are committed in topological order (then by id). Each task is
        validated against the fragments already committed earlier in *this* batch
        (``peers``), so a genuine cross-agent conflict is attributed to the later
        task, which is rejected and retried while the earlier one stands.
        """
        by_id = {c.bundle.task_id: c for c in batch}
        peers: dict[str, str] = {}
        for task_id in _batch_order(wave, list(by_id)):
            completion = by_id[task_id]
            committed = self.process_one(
                wave, completion.bundle, completion.result, peers
            )
            peers.update(committed)
        # Anything this batch completed may have released a lock a parked
        # result was waiting for.
        self.resume_parked(wave)

    def drain(self, wave: WaveState, scheduler: Scheduler) -> None:
        """Process the results of already-dispatched tasks, dispatching nothing.

        Bounded by the number in flight when it starts rather than by
        ``scheduler.dispatched`` emptying: a partially-completed task re-queues
        itself for a narrower re-dispatch, and draining deliberately never makes
        that dispatch, so waiting for the set to empty would wait forever. Those
        re-queued partials are dropped and surface as stranded tasks in the
        result, which is what they are.
        """
        pending = len(scheduler.dispatched - set(wave.parked))
        while pending > 0:
            batch = self.collect()
            if not batch:
                wave.wedged = True
                break
            pending -= len(batch)
            self.process_batch(wave, batch)
        wave.partial_queue.clear()
        # A parked result is waiting on a task that will now never be
        # dispatched again; it is stranded like any other unfinished task.
        wave.parked.clear()

    def resume_parked(self, wave: WaveState) -> None:
        """Retry every parked commit that may now proceed."""
        self._parking.resume(wave, self._settler(wave))

    def release_parked_victim(self, wave: WaveState) -> None:
        """Break a cycle of parked results (see ``ParkedCommits.release_victim``)."""
        self._parking.release_victim(
            wave, wave.require_scheduler(), self._settler(wave)
        )

    def process_one(
        self,
        wave: WaveState,
        bundle: TaskBundle,
        result: TaskResult,
        peers: dict[str, str],
    ) -> dict[str, str]:
        """Validate/commit one result; return the sources it committed (for peers)."""
        task_id = bundle.task_id
        progress = wave.progress[task_id]
        progress.attempts += 1
        reported = dict.fromkeys([*result.modified_nodes, *result.new_sources])
        self._log_agent_result(progress, result, reported)
        accepted: list[NodeId] = []
        if result.success:
            accepted = self._stage_returned_sources(
                task_id, progress.target_nodes, result.new_sources
            )
        elif result.error:
            # The agent call itself failed (API error, or a truncated/malformed
            # structured response). Keep the reason so the run can report it.
            record_failure(wave, task_id, result.error)
        # A node is committable only if a pending fragment actually exists for it —
        # either staged here from the agent's returned source, or put directly by a
        # test/local runner. An id the agent *claims* it changed but provided no
        # source for cannot be committed (the task stays incomplete and retries);
        # the empty-result diagnosis names that case rather than leaving the
        # operator with a symptom.
        in_scope = set(progress.target_nodes)
        staged = [
            n
            for n in dict.fromkeys([*reported, *accepted])
            if n in in_scope and self._view.store.get_staged(n) is not None
        ]
        return self.settle(wave, bundle, result, staged, peers)

    def settle(
        self,
        wave: WaveState,
        bundle: TaskBundle,
        result: TaskResult,
        staged: list[NodeId],
        peers: dict[str, str],
    ) -> dict[str, str]:
        """Commit what can be committed and account the attempt's outcome.

        Split from :meth:`process_one` so a parked result can be settled again
        later without being counted — or logged — as a second agent attempt.
        """
        task_id = bundle.task_id
        progress = wave.progress[task_id]
        wave.deferring.pop(task_id, None)
        committed = (
            self._pipeline.commit(wave, task_id, staged, peers)
            if result.success
            else []
        )
        if task_id in wave.deferring:
            self._parking.park(wave, bundle, result, staged)
            return {}
        committed_sources: dict[str, str] = {}
        for node_id in committed:
            progress.completed_nodes.add(node_id)
            release_lock(self._lock_table, wave, task_id, node_id)
            source = self._view.source(node_id)
            if source is not None:
                committed_sources[str(node_id)] = source

        refusals: list[str] = []
        if self._noop.is_asserted(result):
            refusals = self._noop.accept(wave, progress, result)

        # Nothing was stageable and the task is still open: say *why*, now, while
        # the returned ids are still in hand, instead of a catch-all later.
        if result.success and not staged and not progress.is_complete:
            record_failure(
                wave,
                task_id,
                self._noop.describe_empty_result(progress, result, refusals),
            )

        if progress.is_complete:
            self._retry.finish_task(wave, task_id)
        else:
            self._retry.handle_incomplete(wave, progress, result)
        return committed_sources

    def _settler(self, wave: WaveState) -> Settle:
        """Return :meth:`settle` bound to ``wave``, for resuming parked results."""

        def settle(
            bundle: TaskBundle,
            result: TaskResult,
            staged: list[NodeId],
            peers: dict[str, str],
        ) -> dict[str, str]:
            return self.settle(wave, bundle, result, staged, peers)

        return settle

    def _log_agent_result(
        self,
        progress: SubTaskProgress,
        result: TaskResult,
        reported: dict[NodeId, None],
    ) -> None:
        """Record what the agent actually returned for this attempt.

        Enough to reconstruct a dropped-result failure from the log alone: the
        grant, the ids that came back, how much source came with each, and — the
        field that tells a truncation from a deliberate no-op — the provider's
        own stop reason and token usage.
        """
        self.agent_usage.update(
            {k: v for k, v in result.usage.items() if isinstance(v, int)}
        )
        self._log(
            EventType.AGENT_RESULT,
            task_id=progress.task_id,
            attempt=progress.attempts,
            success=result.success,
            granted=[str(n) for n in progress.target_nodes],
            returned_nodes=[str(n) for n in reported],
            source_lengths={
                str(node_id): len(source)
                for node_id, source in result.new_sources.items()
            },
            no_changes_required=result.no_changes_required,
            stop_reason=result.stop_reason,
            usage=dict(result.usage),
            repairs=result.repairs,
            error=result.error,
        )

    def _stage_returned_sources(
        self, task_id: str, grant: list[NodeId], new_sources: dict[NodeId, str]
    ) -> list[NodeId]:
        """Stage each rewritten source the agent returned; return the ids staged.

        This is the agent→store transport: an API/CLI agent reports the full new
        source of each node it changed, and the session ``put_node``s it (as a new
        pending version) so the normal validate→commit path applies it.

        An agent may not edit beyond the nodes it was authorized to modify, so a
        source outside the grant is refused — but *loudly*: every refusal is
        logged with the id, the grant, and the reason, so a granularity mismatch
        is diagnosable from the log rather than surfacing as unexplained retries.
        """
        store = self._view.store
        # order_key: when several fragments fold into one whole-file grant they
        # are concatenated, and concatenating them in the order the model
        # happened to emit puts imports after code. That still compiles, so every
        # downstream gate passes it — the store's own source order is the
        # authority, and the header leads regardless.
        accepted, dropped = map_returned_sources(
            grant, new_sources, order_key=store.node_order
        )
        for node_id, source in accepted.items():
            store.put_node(
                node_id, NodeFragment(node_id, self._view.kind(node_id), source, 1)
            )
        for node_id, reason in dropped:
            self._log(
                EventType.SOURCE_DROPPED,
                task_id=task_id,
                node_id=str(node_id),
                granted=[str(n) for n in grant],
                source_length=len(new_sources[node_id]),
                reason=reason,
            )
        return list(accepted)


def _batch_order(wave: WaveState, task_ids: list[str]) -> list[str]:
    """Order a batch's task ids by topological index, then id (deterministic)."""
    order = wave.require_scheduler().dag.topological_order()
    index = {tid: i for i, tid in enumerate(order)}
    return sorted(set(task_ids), key=lambda t: (index.get(t, len(index)), t))
