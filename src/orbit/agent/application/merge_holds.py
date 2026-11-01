"""The holds on merges into the user's branch: a run of a set that holds merges failing holds them,
with a notice, and queues a coder's fix at the front of the queue; once the fix lands, the set runs
again, and passing lifts its hold, as the user releasing every hold by hand does. Holds are kept
per repository, beside the review count, so they outlive a restart.

While any hold is on, the work lane commits each task's verified work in the coder's tree and
leaves it there, waiting, rather than merging it; a fix merges with the work stacked beneath it.
Once no hold is left, :attr:`MergeHolds.released` is told, and the work waiting lands.
"""

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.ports import CoderTree, Publish, SessionStore, StoreFailed, TreeFailed
from orbit.agent.application.set_runs import SetResult
from orbit.agent.application.storage_halt import Halting, written
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.merge_holds import (
    Hold,
    decide_failure,
    find_hold,
    fix_root,
    fixed_set,
    hold_view,
    kept_holds,
    lift_hold,
    merge_holds,
    released_note,
    resumed_note,
    set_fix_prompt,
)
from orbit.agent.domain.roles import Role, role_task
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.verification import VerificationStep, failure_report
from orbit.protocol import CODER, INTERNAL, MergeHold, Notice, TaskState

_FIXING = frozenset({"pending", "running", "verifying"})
"""The statuses of a task that's still to fix what it fixes."""


type _Read = Literal["unread", "read"]
"""Whether the holds kept have been read since the last restore began: none is saved until they
have, so holds started without them can't replace those kept."""


def _unwatched() -> None:
    """Note a change no one watches."""


@dataclass(frozen=True)
class HoldParts:
    """What the holds act on: the ``tasks`` the fixes are queued on, with the session's
    ``roles``, asked afresh, the coder's among them, the ``tree`` whose commits a fix lists,
    ``publish``, which tells the user, the ``store`` the holds are kept in, if any, the
    session's ``halt``, which a failing save halts, ``kick``, which runs the tasks waiting in a
    role's lane if it starts by itself, and ``rerun``, which runs a set again, by name, as the
    trigger named second fired."""

    tasks: Tasks
    roles: Callable[[], Mapping[str, Role]]
    tree: CoderTree
    publish: Publish
    store: SessionStore | None
    halt: Halting
    kick: Callable[[str], None]
    rerun: Callable[[str, str], None]


class MergeHolds:
    """The holds of the sets ``holding`` merges, by name, each queuing at most ``limit`` fixes,
    acting on ``parts``. :attr:`changed` is told whenever the holds change, and
    :attr:`released` once the last lifts. A failing read of the holds kept is reported, and a
    failing save halts the session. Nothing is saved while it's halted, nor until a read of the
    holds kept has worked, so holds started without them can't replace those kept."""

    write = FailedWrite.MERGE_HOLDS

    def __init__(self, holding: Collection[str], limit: int, parts: HoldParts) -> None:
        self._holding = frozenset(holding)
        self._limit = limit
        self._parts = parts
        self._board = parts.tasks.board
        self._reads = ReportedOnce(parts.publish, INTERNAL)
        self._holds: tuple[Hold, ...] = ()
        self._kept: tuple[Hold, ...] = ()  # The holds the store held when last read or saved.
        self._read: _Read = "unread"
        self._passed: dict[str, str] = {}  # Set name -> the commit it last passed on.
        self._saving = asyncio.Lock()  # Saves once at a time, so the newest is written last.
        self._loading = asyncio.Lock()  # Reads the holds kept once at a time, the newest last.
        self.changed: Callable[[], None] = _unwatched
        self.released: Callable[[], None] = _unwatched

    @property
    def held(self) -> bool:
        """Whether any hold is on."""
        return bool(self._holds)

    @property
    def holds(self) -> tuple[Hold, ...]:
        """The holds on, in the order they started."""
        return self._holds

    @property
    def views(self) -> tuple[MergeHold, ...]:
        """The holds on, as frontends see them."""
        return tuple(hold_view(hold) for hold in self._holds)

    async def restore(self) -> None:
        """Take on the holds kept, read afresh, as another orbit may have changed them since
        they were last read, those of sets no longer holding merges dropped, unless they can't be
        read; a hold started since they were last read replaces the one kept for its set.
        Nothing is saved from now until they've been read."""
        store = self._parts.store
        if store is None:
            return
        async with self._loading:
            self._read = "unread"
            loaded = await self._reads.value(store.merge_holds())
            if loaded is not None:
                await self._take_on(store, loaded)

    async def _take_on(self, store: SessionStore, loaded: tuple[Hold, ...]) -> None:
        """Take on ``loaded``, the holds ``store`` keeps, as :meth:`restore` says, saving the
        result if it differs and telling :attr:`released` if no hold is left of those on."""
        held = self.held
        started = tuple(hold for hold in self._holds if hold not in self._kept)
        self._kept = loaded
        self._read = "read"
        self._holds = merge_holds(kept_holds(loaded, self._holding), started)
        self.changed()
        if self._holds != loaded:  # Holds dropped, or started since they were last read.
            await self._save(store)
        if held and not self._holds:
            self.released()

    async def heard(self, result: SetResult) -> None:
        """Act on how a run of a set that holds merges came out, as ``result`` says: passing
        lifts its hold, if any, and failing holds merges, as
        :func:`~orbit.agent.domain.merge_holds.decide_failure` says."""
        name = result.check_set.name
        if result.failure is None:
            await self._lift(name, result.commit)
            return
        step, output = result.failure
        failed = Hold(name, result.commit, step.name, output)
        decision = decide_failure(self._holds, failed, self._limit, self._fixing(name))
        await self._replace(decision.holds)
        for note in decision.notes:
            await self._parts.publish(Notice(message=note))
        if decision.fix:
            await self._queue_fix(find_hold(decision.holds, name) or failed, step, result.trigger)

    def fix_of(self, state: TaskState) -> str | None:
        """The set held whose fix the task that is ``state`` is, or carries on from; none if
        it's no such fix."""
        name = self._fixes(state)
        return name if name is not None and find_hold(self._holds, name) else None

    def fix_landed(self, name: str, trigger: str) -> None:
        """A fix for the set ``name``, queued by the trigger named ``trigger``, landed: run the
        set again, on the new commit, to see whether its hold lifts."""
        self._parts.rerun(name, trigger)

    async def release(self) -> None:
        """Lift every hold, as the user asked by hand, telling them which sets haven't passed,
        and :attr:`released`; nothing while none is on. A set that fails again starts a new
        hold."""
        names = tuple(hold.name for hold in self._holds)
        if not names:
            return
        await self._replace(())
        await self._parts.publish(Notice(message=released_note(names)))
        self.released()

    async def retry(self) -> StoreFailed | None:
        """Save the holds, though the session is halted, as resuming does, unless those kept
        haven't been read; returns the error when the save fails."""
        if self._parts.store is None:
            return None
        async with self._saving:
            return await self._write(self._parts.store)

    async def _lift(self, name: str, commit: str) -> None:
        """Note that the set ``name`` passed on ``commit``, and lift its hold, if any, telling
        :attr:`released` once no hold is left."""
        self._passed[name] = commit
        if find_hold(self._holds, name) is None:
            return
        await self._replace(lift_hold(self._holds, name))
        await self._parts.publish(Notice(message=resumed_note(name)))
        if not self._holds:
            self.released()

    def _fixing(self, name: str) -> bool:
        """Whether a fix for the set ``name`` is still to run or running."""
        return any(
            state.status in _FIXING and self._fixes(state) == name for state in self._board.tasks
        )

    def _fixes(self, state: TaskState) -> str | None:
        """The set the task that is ``state`` fixes, or carries on fixing; none if none."""
        return fixed_set(fix_root(state, self._board.find).task.prompt)

    async def _queue_fix(self, hold: Hold, step: VerificationStep, trigger: str) -> None:
        """Queue a coder's task fixing ``hold``'s set, which failed at ``step``, at the front of
        the queue, from the trigger named ``trigger``, and run the coder's lane if it starts by
        itself; nothing without a coder."""
        coder = self._parts.roles().get(CODER)
        if coder is None:
            return
        report = failure_report(step, hold.output)
        task = role_task(CODER, coder, set_fix_prompt(hold, await self._subjects(hold), report))
        await self._parts.tasks.change(lambda board: board.add_first(task, trigger))
        self._parts.kick(CODER)

    async def _subjects(self, hold: Hold) -> list[str] | None:
        """The subjects of the commits up to the one ``hold``'s set failed on since it last
        passed; none if it hasn't passed since orbit started, or git can't list them."""
        passed = self._passed.get(hold.name)
        if passed is None:
            return None
        try:
            return await self._parts.tree.landed_since(passed, hold.commit)
        except TreeFailed:
            return None

    async def _replace(self, holds: tuple[Hold, ...]) -> None:
        """Have ``holds`` be the holds on, telling :attr:`changed`, and save them, if they
        changed and there's a store."""
        changed = holds != self._holds
        self._holds = holds
        self.changed()
        if changed and self._parts.store is not None:
            await self._save(self._parts.store)

    async def _save(self, store: SessionStore) -> None:
        """Save the holds on in ``store``, unless the session is halted, halting it if the save
        fails."""
        async with self._saving:
            await written(self._parts.halt, self.write, lambda: self._write(store))

    async def _write(self, store: SessionStore) -> StoreFailed | None:
        """Save the holds on in ``store``, unless those kept haven't been read; returns the error
        when it fails."""
        if self._read == "unread":
            return None
        holds = self._holds
        error = await outcome(store.save_merge_holds(holds))
        if isinstance(error, StoreFailed):
            return error
        self._kept = holds
        return None
