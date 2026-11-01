"""The holds on merges into the user's branch: a run of a set that holds merges failing holds them,
with a notice, and queues a coder's fix at the front of the queue; once the fix lands, the set runs
again, and passing lifts its hold, as the user releasing every hold by hand does. A chain of
fixes that ends without its work merging counts as the set failing again, as it last did; a fix
that ends in error hasn't ended its chain, as it's left to run again. Holds are kept per
repository, beside the review count, so they outlive a restart.

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
from orbit.agent.domain.board import CUT_SHORT, WAITING
from orbit.agent.domain.merge_holds import (
    NONE_LIFTED,
    Hold,
    HoldDecision,
    Lifted,
    decide_failure,
    decide_unlanded_fix,
    find_hold,
    fix_root,
    fixed_set,
    fold_holds,
    held_step,
    hold_view,
    lift_hold,
    note_lift,
    released_note,
    resumed_note,
    set_fix_prompt,
    unlifted_sets,
)
from orbit.agent.domain.roles import Role, role_task
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.verification import CheckSet, VerificationStep, failure_report
from orbit.protocol import CODER, INTERNAL, MergeHold, Notice, TaskState

_FIXING = WAITING | CUT_SHORT
"""The statuses of a task that's still to fix what it fixes: one yet to finish, or one cut short by
an error, the user or a crash, which the board runs again first."""


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
    """The holds of the sets ``holding`` merges, until :meth:`use_sets` gives others, each
    queuing at most ``limit`` fixes, acting on ``parts``. :attr:`changed` is told whenever the
    holds change, and :attr:`released` once the last lifts. A failing read of the holds kept is
    reported, and a failing save halts the session. Nothing is saved while it's halted, nor
    until a read of the holds kept has worked, so holds started without them can't replace those
    kept."""

    write = FailedWrite.MERGE_HOLDS

    def __init__(self, holding: Collection[CheckSet], limit: int, parts: HoldParts) -> None:
        self._sets = tuple(holding)
        self._holding = frozenset(check_set.name for check_set in holding)
        self._limit = limit
        self._parts = parts
        self._board = parts.tasks.board
        self._reads = ReportedOnce(parts.publish, INTERNAL)
        self._holds: tuple[Hold, ...] = ()
        self._kept: tuple[Hold, ...] = ()  # The holds the store held when last read or saved.
        self._lifted: Lifted = NONE_LIFTED  # Those lifted here since then, which it may still hold.
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
    def is_known(self) -> bool:
        """Whether the holds on are known, so :attr:`held` misses none: there's no store keeping
        any, those it keeps were read since the last restore began, or every hold they could
        add was lifted since, as releasing them by hand lifts all."""
        unread = self._read == "unread" and bool(unlifted_sets(self._holding, self._lifted))
        return self._parts.store is None or not unread

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
        read; a hold lifted here since they were last read or saved is dropped, and one started
        since then replaces the one kept for its set. Nothing is saved from now until they've
        been read, and :attr:`released` is told once they have if no hold is left."""
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
        result if it differs and telling :attr:`released` if no hold is left, as work saved
        waiting for one that has gone lands then."""
        started = tuple(hold for hold in self._holds if hold not in self._kept)
        self._holds = fold_holds(loaded, self._holding, self._lifted, started)
        self._kept = loaded
        self._lifted = NONE_LIFTED
        self._read = "read"
        self.changed()
        if self._holds != loaded:  # Holds dropped, lifted, or started since they were last read.
            await self._save(store)
        if not self._holds:
            self.released()

    async def use_sets(self, holding: Collection[CheckSet]) -> None:
        """Have the sets ``holding`` merges hold them from now on: a set that comes to hold them
        holds them as it next fails, and the hold of one that no longer does is dropped, as it
        would never lift, telling :attr:`released` if no hold is left."""
        self._sets = tuple(holding)
        self._holding = frozenset(check_set.name for check_set in holding)
        kept = tuple(hold for hold in self._holds if hold.name in self._holding)
        if kept == self._holds:
            return
        await self._replace(kept)
        if not kept:
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
        fixed = await self._decide(decision, name)
        if fixed is not None:
            await self._queue_fix(fixed, step, result.trigger)

    async def hear_unlanded_fix(self, state: TaskState) -> None:
        """Act on the task that ended as ``state`` without its work merging, if it's a fix for
        a set held, or carries on from one: once no fix for that set is still to run, running,
        or ended in error, its hold failed again, as it last did, as
        :func:`~orbit.agent.domain.merge_holds.decide_unlanded_fix` says, the next fix queued
        from the trigger that queued this one. The set runs again instead if the step it failed
        at is gone from it, as there's no failure left to fix."""
        name = self._fixes(state)
        hold = None if name is None else find_hold(self._holds, name)
        if hold is None:
            return
        step = held_step(self._sets, hold)
        if step is None:
            self._parts.rerun(hold.name, state.origin)
            return
        decision = decide_unlanded_fix(self._holds, hold.name, self._limit, self._fixing(hold.name))
        fixed = await self._decide(decision, hold.name)
        if fixed is not None:
            await self._queue_fix(fixed, step, state.origin)

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
        self._lifted = "every"
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
        held = find_hold(self._holds, name)
        if held is not None or self._read == "unread":  # The store may hold it unread.
            self._lifted = note_lift(self._lifted, name)
        if held is None:
            return
        await self._replace(lift_hold(self._holds, name))
        await self._parts.publish(Notice(message=resumed_note(name)))
        if not self._holds:
            self.released()

    async def _decide(self, decision: HoldDecision, name: str) -> Hold | None:
        """Have the holds be as ``decision`` says, telling the user its notes; returns the set
        ``name``'s hold if a fix for it is to be queued, none otherwise."""
        await self._replace(decision.holds)
        for note in decision.notes:
            await self._parts.publish(Notice(message=note))
        return find_hold(decision.holds, name) if decision.fix else None

    def _fixing(self, name: str) -> bool:
        """Whether a fix for the set ``name`` is still to run, running, or cut short by an error,
        the user or a crash, which leaves it to run again."""
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
        when it fails. Once it's done the store holds none of the holds lifted before it, so they
        needn't be dropped from the next read, unless more were lifted meanwhile, which a later
        save writes."""
        if self._read == "unread":
            return None
        holds, lifted = self._holds, self._lifted
        error = await outcome(store.save_merge_holds(holds))
        if isinstance(error, StoreFailed):
            return error
        self._kept = holds
        if self._lifted == lifted:
            self._lifted = NONE_LIFTED
        return None
