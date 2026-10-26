"""Switching a session between saved ones: listing, renaming and deleting them, and activating
another, or a new one, in place of the one open."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from orbit.agent.application.connection import to_everyone
from orbit.agent.application.conversation_seats import ConversationSeats
from orbit.agent.application.keeper import (
    ALREADY_OPEN,
    AlreadyOpen,
    Contents,
    Left,
    Opened,
    SessionKeeper,
)
from orbit.agent.application.outbox import EventSink, Shown
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.activity import Activity, switch_refusal
from orbit.agent.domain.board import interrupted
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.role_spend import RoleSpend
from orbit.agent.domain.sessions import (
    EMPTY_NAME,
    SessionRecord,
    resumed_notes,
    session_name,
)
from orbit.protocol import (
    BUSY,
    INVALID,
    ActivateSession,
    DeleteSession,
    Event,
    NewSession,
    Notice,
    RenameSession,
    SessionSummary,
    TaskState,
    Usage,
    WorkspaceEntry,
)

NO_REVIEWS = LaneSnapshot(planner=PlannerSnapshot())
"""The review state of a session that has had none."""

SessionCommand = ActivateSession | NewSession | RenameSession | DeleteSession


def _stopped(saved: Sequence[TaskState], now: Sequence[TaskState]) -> list[str]:
    """The ids of the tasks ``saved`` that are otherwise ``now``, as they were interrupted."""
    return [task.id for task, was in zip(now, saved, strict=True) if task != was]


@dataclass(frozen=True)
class SwitchParts:
    """What a switch replaces the contents of: the ``keeper`` saving the session, its ``tasks``,
    its conversational roles' ``seats``, its review ``lane`` and ``work`` lane, if any, and the
    ``activity`` now in progress there; the ``snapshot`` of the session
    open, last active then, that follows a switch; the workspace's ``entry`` in the catalog,
    holding the sessions given, which ``broadcast`` sends every listening connection, in the
    catalog, whenever they change; and
    ``switching``, called as a switch is no longer refused, just before the session open is
    another, with whether the session left stays saved."""

    keeper: SessionKeeper
    tasks: Tasks
    seats: ConversationSeats
    lane: ReviewLane | None
    work: WorkLane | None
    activity: Callable[[], Activity]
    snapshot: Callable[[datetime | None], Awaitable[Shown]]
    entry: Callable[[tuple[SessionSummary, ...]], WorkspaceEntry]
    broadcast: Callable[[WorkspaceEntry], Awaitable[None]]
    switching: Callable[[bool], None]


class SessionSwitcher:
    """Switches the session held in ``parts`` with ``settings`` between saved ones, publishing
    into ``events``; it also holds what the session's agents have spent.

    While ``picking``, no session was picked yet: what orbit adds to the one open carries over
    into the one picked.
    """

    def __init__(
        self,
        parts: SwitchParts,
        settings: SessionSettings,
        events: EventSink,
        picking: bool = False,
    ) -> None:
        self._parts = parts
        self._keeper = parts.keeper
        self._settings = settings
        self._events = events
        self._picking = picking
        self.spent = Usage()  # What the session's agents have spent, the planner's included.
        self.spend = RoleSpend()  # The same, by role and by the role consulting.
        # What the opened session saved that no review lane here holds, kept to save back.
        self._kept_reviews: LaneSnapshot | None = None
        # What orbit adds between switches waits for those under way, keeping new ones waiting,
        # so it lands in exactly one session.
        self._adding = asyncio.Lock()
        self._under_way = 0
        self._settled = asyncio.Event()
        self._settled.set()

    def stay(self) -> None:
        """Note that the user works on in this session, so what orbit added stays in it."""
        self._picking = False

    async def handle(self, command: SessionCommand) -> None:
        match command:
            case ActivateSession() | NewSession():
                await self.activate(command)
            case RenameSession() | DeleteSession():
                await self.change_saved(command)
            case _:
                assert_never(command)

    async def activate(self, command: ActivateSession | NewSession) -> str | AlreadyOpen | None:
        """Make the saved session ``command`` names, or a new one, the one open here; returns
        the id of the session the switch left if its lock was held, then let go of or, if no
        session was picked yet, deleted; :data:`ALREADY_OPEN` if the session named is the one
        open; or none."""
        async with self._switch():
            if isinstance(command, ActivateSession):
                return await self._open_session(command.session)
            return await self._new_session(command.name)

    @asynccontextmanager
    async def _switch(self) -> AsyncIterator[None]:
        """Have a switch under way, once what orbit adds between switches is added."""
        async with self._adding:
            self._under_way += 1
            self._settled.clear()
        try:
            yield
        finally:
            self._under_way -= 1
            if not self._under_way:
                self._settled.set()

    async def between_switches[T](self, change: Callable[[], Awaitable[T]]) -> T:
        """Make ``change`` to the session open once no switch is under way, no other starting
        until it's made, so what it adds lands in the session open when it's made, the one a
        switch under way opens; returns what ``change`` returned."""
        async with self._adding:
            await self._settled.wait()
            return await change()

    async def entry(self) -> WorkspaceEntry:
        """The workspace's entry in the catalog, holding the sessions as they are now."""
        return self._parts.entry(await self._sessions())

    async def publish_catalog(self) -> None:
        """Send the catalog, holding the sessions as they are now, to every listening
        connection, whichever session it views."""
        await self._parts.broadcast(await self.entry())

    async def _to_everyone(self, event: Event) -> None:
        """Publish ``event``, the session's new state, to every connection that views it."""
        with to_everyone():
            await self._events.put(event)

    async def _sessions(self) -> tuple[SessionSummary, ...]:
        """The saved sessions, as the list of sessions shows them, after the one open here if
        it isn't saved."""
        saved = await self._keeper.summaries()
        if any(summary.current for summary in saved):
            return saved
        return (self.summary(), *saved)

    def contents(self) -> Contents:
        """What the session holds now, to save, with what it was opened with in place of the
        reviews it has no lane to hold."""
        lane = self._parts.lane
        return Contents(
            role_settings=self._settings.roles,
            conversations=self._parts.seats.conversations,
            tasks=self._parts.tasks.board.tasks,
            reviews=lane.snapshot() if lane else self._kept_reviews,
            spent=self.spent,
            planner_notes=self._parts.seats.notes,
            copies=self._parts.seats.copies,
            consultations=self._parts.seats.consultations,
            last_consultation=self._parts.seats.last_consultation,
            spend=self.spend,
        )

    async def save(self) -> None:
        """Save the session, if it's used, as it is now."""
        await self._keeper.save(self.contents)

    async def change_saved(self, command: RenameSession | DeleteSession) -> bool:
        """Rename or delete a session and publish the catalog; returns whether the session was
        renamed or deleted."""
        match command:
            case RenameSession(session_id=session_id, name=name):
                changed = await self._rename_session(session_id, name)
            case DeleteSession(session_id=session_id):
                changed = await self._keeper.delete_saved(session_id)
            case _:
                assert_never(command)
        await self.publish_catalog()
        return changed

    async def _rename_session(self, session_id: str, name: str) -> bool:
        """Name the session ``session_id`` ``name``, saving it if it's saved; returns whether
        it was renamed. Refused for an empty name."""
        named = session_name(name)
        if named is None:
            raise Refused(INVALID, EMPTY_NAME)
        if session_id != self._keeper.id:
            return await self._keeper.rename_saved(session_id, named)
        self._keeper.rename(named)
        await self.save()
        return True

    async def _open_session(self, session_id: str) -> str | AlreadyOpen | None:
        """Open the saved session ``session_id`` in place of this one, without running
        anything: what was running when it was saved is cancelled; returns the id of the
        session left if its lock was held, then let go of or deleted, or none, as
        :meth:`activate` does. Refused while anything runs, or if it can't be opened; opening
        this session does nothing, returning :data:`ALREADY_OPEN`."""
        if session_id == self._keeper.id:
            return ALREADY_OPEN
        self._may_switch()
        await self._leave_consultations()
        opened = await self._keeper.open(session_id, self._load)
        if not isinstance(opened, Opened):
            return opened
        record = opened.record
        tasks, count = interrupted(record.tasks)
        await self._switched(opened.left, _stopped(record.tasks, tasks), record.last_active)
        for note in resumed_notes(record, count):
            await self._events.put(Notice(message=note))
        return opened.left.id

    async def _new_session(self, name: str | None) -> str | None:
        """Start a new session ``name``d so in place of this one; returns the id of the session
        left if its lock was held, then let go of or deleted, or none, as :meth:`activate`
        does. Refused while anything runs."""
        self._may_switch()
        await self._leave_consultations()
        left = await self._keeper.renew(name, self._clear)
        await self._switched(left, (), None)
        return left.id

    async def _leave_consultations(self) -> None:
        """Save the session with its consultations pending as interrupted by a switch, to
        answer as it opens again; a switch refused after leaves them to run on, answered."""
        self._parts.seats.leave()
        await self.save()

    def _may_switch(self) -> None:
        """Refuse a switch, raising :class:`Refused`, while anything runs, saying what runs."""
        refusal = switch_refusal(self._parts.activity())
        if refusal is not None:
            raise Refused(BUSY, refusal)

    def _load(self, record: SessionRecord) -> None:
        """Hold what ``record`` does, its tasks as interrupted; refused while anything runs."""
        self._may_switch()
        self._leaving()
        tasks = interrupted(record.tasks)[0]
        self.spent = record.spent
        self.spend = RoleSpend(record.role_spent, record.caused_spent)
        self._settings.resume(record.role_settings)
        self._hold(tasks, record)

    def _clear(self) -> None:
        """Hold what a new session does, keeping the settings and having spent nothing; refused
        while anything runs."""
        self._may_switch()
        self._leaving()
        self.spent = Usage()
        self.spend = RoleSpend()
        self._hold((), None)

    def _leaving(self) -> None:
        """Say a switch is under way, with whether the session left stays saved: it does once
        it's saved, unless no session was picked yet, as then it's deleted."""
        self._parts.switching(self._keeper.kept and not self._picking)

    def _hold(self, tasks: tuple[TaskState, ...], record: SessionRecord | None) -> None:
        """Hold a session's ``tasks`` and what else its ``record`` holds, if it has one: its
        roles' conversations, reviews, the notes its planner is still to hear, its roles' copies
        and its consultations pending; with no fix counts."""
        parts = self._parts
        self._replace_board(tasks)
        if parts.work is not None:
            parts.work.reset()
        if record is None:
            parts.seats.replace({})
        else:
            parts.seats.replace(
                record.conversations,
                record.planner_notes,
                record.copies,
                record.consultations,
                record.last_consultation,
            )
        reviews = record.reviews if record else None
        if parts.lane is not None:
            parts.lane.load(reviews or NO_REVIEWS)
        self._kept_reviews = reviews

    def _replace_board(self, tasks: tuple[TaskState, ...]) -> None:
        """Have ``tasks`` on the board, and after them, while no session was picked yet, the
        tasks orbit added."""
        # Published in the snapshot of the session opened, in _switched, not half-switched here.
        board = self._parts.tasks.board
        carried = board.tasks if self._picking else ()
        board.restore(tasks)
        for state in carried:
            board.append(state)

    async def _switched(
        self, left: Left, stopped: Collection[str], last_active: datetime | None
    ) -> None:
        """Finish a switch from the session ``left``, deleting it if its tasks carried over:
        close its conversations, end the reviews ``stopped`` when it was saved, then save the
        session opened and publish to every connection the catalog and its snapshot, last active
        at ``last_active``, which alone shows what ending the reviews changed, and then note a
        review pause."""
        parts = self._parts
        discard, self._picking = self._picking, False
        await self._keeper.leave(left, discard)
        await parts.seats.close_retired()
        if parts.lane is not None:
            await parts.lane.end_interrupted(stopped)
        await parts.tasks.changed_elsewhere()
        await self.publish_catalog()
        await (await parts.snapshot(last_active)).publish(self._to_everyone)
        await parts.seats.settle()
        if parts.lane is not None:
            await parts.lane.note_pause()

    def summary(self, last_active: datetime | None = None) -> SessionSummary:
        """The session open here, last active at ``last_active``, as the list of sessions
        shows it."""
        return self._keeper.summary(self._parts.tasks.board.tasks, self.spent, last_active)
