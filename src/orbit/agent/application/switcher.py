"""Switching a session between saved ones: listing, renaming and deleting them, and opening
another, or a new one, in place of the one open."""

import asyncio
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from orbit.agent.application.keeper import Contents, Left, SessionKeeper
from orbit.agent.application.planner_seat import PlannerSeat
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.board import interrupted
from orbit.agent.domain.reviews import LaneSnapshot, PlannerSnapshot
from orbit.agent.domain.sessions import (
    EMPTY_NAME,
    SessionRecord,
    resumed_notes,
    resumed_settings,
    session_name,
)
from orbit.protocol import (
    DeleteSession,
    ErrorOccurred,
    Event,
    ListingReason,
    ListSessions,
    NewSession,
    Notice,
    OpenSession,
    RenameSession,
    SessionOpened,
    SessionsListed,
    Settings,
    SettingsChanged,
    TaskState,
    Usage,
)

TASKS_RUNNING = "Can't switch: tasks are running. Press ctrl+s to stop them."
REVIEW_RUNNING = "Can't switch: a review is running. Press c on it in the agents pane to stop it."
PLANNER_ANSWERING = "Can't switch: the planner is answering. Press ctrl+x to stop it."
NO_REVIEWS = LaneSnapshot(planner=PlannerSnapshot())
"""The review state of a session that has had none."""

SessionCommand = ListSessions | OpenSession | NewSession | RenameSession | DeleteSession


def _stopped(saved: Sequence[TaskState], now: Sequence[TaskState]) -> list[str]:
    """The ids of the tasks ``saved`` that are otherwise ``now``, as they were interrupted."""
    return [task.id for task, was in zip(now, saved, strict=True) if task != was]


@dataclass(frozen=True)
class SwitchParts:
    """What a switch replaces the contents of: the ``keeper`` saving the session, its ``tasks``,
    its planner ``seat``, its review ``lane`` and ``work`` lane, if any, and whether
    ``tasks_running`` now."""

    keeper: SessionKeeper
    tasks: Tasks
    seat: PlannerSeat
    lane: ReviewLane | None
    work: WorkLane | None
    tasks_running: Callable[[], bool]


@dataclass(frozen=True)
class SettingsAccess:
    """The session's settings: to ``get`` and ``set`` them, and the event saying they
    ``changed``. A session opened has its saved settings, but for those ``pinned``, by field
    name, which keep the ``given`` ones."""

    get: Callable[[], Settings]
    set: Callable[[Settings], None]
    changed: Callable[[], SettingsChanged]
    given: Settings
    pinned: tuple[str, ...]


class SessionSwitcher:
    """Switches the session held in ``parts`` with ``settings`` between saved ones, publishing
    into ``events``; it also holds what the session's agents have spent.

    While ``picking``, no session was picked yet: what orbit adds to the one open carries over
    into the one picked.
    """

    def __init__(
        self,
        parts: SwitchParts,
        settings: SettingsAccess,
        events: "asyncio.Queue[Event]",
        picking: bool = False,
    ) -> None:
        self._parts = parts
        self._keeper = parts.keeper
        self._settings = settings
        self._events = events
        self._picking = picking
        self.spent = Usage()  # What the session's agents have spent, the planner's included.
        # What the opened session saved that no review lane here holds, kept to save back.
        self._kept_reviews: LaneSnapshot | None = None

    def stay(self) -> None:
        """Note that the user works on in this session, so what orbit added stays in it."""
        self._picking = False

    async def handle(self, command: SessionCommand) -> None:
        match command:
            case ListSessions():
                await self.list_sessions("asked")
            case OpenSession(session_id=session_id):
                await self._open_session(session_id)
            case NewSession(name=name):
                await self._new_session(name)
            case RenameSession() | DeleteSession():
                await self._change_saved(command)
            case _:
                assert_never(command)

    async def list_sessions(self, reason: ListingReason) -> None:
        """Publish the saved sessions, listed for ``reason``."""
        summaries = await self._keeper.summaries()
        await self._events.put(SessionsListed(sessions=summaries, reason=reason))

    def contents(self) -> Contents:
        """What the session holds now, to save, with what it was opened with in place of the
        reviews it has no lane to hold."""
        lane = self._parts.lane
        return Contents(
            settings=self._settings.get(),
            planner_conversation=self._parts.seat.conversation_id,
            tasks=self._parts.tasks.board.tasks,
            reviews=lane.snapshot() if lane else self._kept_reviews,
            spent=self.spent,
        )

    async def save(self) -> None:
        """Save the session, if it's used, as it is now."""
        await self._keeper.save(self.contents)

    async def _change_saved(self, command: RenameSession | DeleteSession) -> None:
        """Rename or delete a session, then list the sessions as they are after."""
        match command:
            case RenameSession(session_id=session_id, name=name):
                await self._rename_session(session_id, name)
            case DeleteSession(session_id=session_id):
                await self._keeper.delete_saved(session_id)
            case _:
                assert_never(command)
        await self.list_sessions("refreshed")

    async def _rename_session(self, session_id: str, name: str) -> None:
        """Name the session ``session_id`` ``name``, saving it if it's saved; refused for an
        empty name."""
        named = session_name(name)
        if named is None:
            await self._events.put(ErrorOccurred(message=EMPTY_NAME))
        elif session_id == self._keeper.id:
            self._keeper.rename(named)
            await self.save()
        else:
            await self._keeper.rename_saved(session_id, named)

    async def _open_session(self, session_id: str) -> None:
        """Open the saved session ``session_id`` in place of this one, without running
        anything: what was running when it was saved is cancelled. Refused while anything runs,
        or if it can't be opened; opening this session does nothing."""
        if session_id == self._keeper.id or not self._may_switch():
            return
        opened = await self._keeper.open(
            session_id, lambda record: self._may_switch() and self._load(record)
        )
        if opened is None:
            return
        record = opened.record
        tasks, count = interrupted(record.tasks)
        await self._switched(opened.left, _stopped(record.tasks, tasks), record.last_active)
        for note in resumed_notes(record, count):
            await self._events.put(Notice(message=note))

    async def _new_session(self, name: str | None) -> None:
        """Start a new session ``name``d so in place of this one; refused while anything
        runs."""
        left = await self._keeper.renew(name, lambda: self._may_switch() and self._clear())
        if left is not None:
            await self._switched(left, (), None)

    def _may_switch(self) -> bool:
        """Whether the session may be switched now, as nothing runs; otherwise says what runs."""
        lane = self._parts.lane
        if self._parts.tasks_running():
            refusal = TASKS_RUNNING
        elif lane is not None and lane.running:
            refusal = REVIEW_RUNNING
        elif self._parts.seat.running:
            refusal = PLANNER_ANSWERING
        else:
            return True
        self._events.put_nowait(ErrorOccurred(message=refusal))
        return False

    def _load(self, record: SessionRecord) -> bool:
        """Hold what ``record`` does, its tasks as interrupted; returns True."""
        given = self._settings
        settings = resumed_settings(record.settings, given.given, given.pinned)
        tasks = interrupted(record.tasks)[0]
        self.spent = record.spent
        return self._hold(tasks, settings, record.planner_conversation, record.reviews)

    def _clear(self) -> bool:
        """Hold what a new session does, keeping the settings and having spent nothing; returns
        True."""
        self.spent = Usage()
        return self._hold((), self._settings.get(), None, None)

    def _hold(
        self,
        tasks: tuple[TaskState, ...],
        settings: Settings,
        conversation: str | None,
        reviews: LaneSnapshot | None,
    ) -> bool:
        """Hold a session's ``tasks``, ``settings``, planner ``conversation`` and ``reviews``,
        with no fix counts; returns True."""
        parts = self._parts
        self._replace_board(tasks)
        if parts.work is not None:
            parts.work.reset()
        self._settings.set(settings)
        parts.seat.replace(conversation)
        if parts.lane is not None:
            parts.lane.load(reviews or NO_REVIEWS)
        self._kept_reviews = reviews
        return True

    def _replace_board(self, tasks: tuple[TaskState, ...]) -> None:
        """Have ``tasks`` on the board, and after them, while no session was picked yet, the
        tasks orbit added."""
        # Published with the rest of the session opened, in _switched, not half-switched here.
        board = self._parts.tasks.board
        carried = board.tasks if self._picking else ()
        board.restore(tasks)
        for state in carried:
            board.append(state)

    async def _switched(
        self, left: Left, stopped: Collection[str], last_active: datetime | None
    ) -> None:
        """Finish a switch from the session ``left``, deleting it if its tasks carried over:
        close its planner, end the reviews ``stopped`` when it was saved, and publish what the
        session opened, last active at ``last_active``, holds."""
        parts = self._parts
        discard, self._picking = self._picking, False
        await self._keeper.leave(left, discard)
        await parts.seat.close_retired()
        if parts.lane is not None:
            await parts.lane.end_interrupted(stopped)
        await parts.tasks.publish()
        await self._events.put(self._settings.changed())
        summary = self._keeper.summary(parts.tasks.board.tasks, self.spent, last_active)
        await self._events.put(SessionOpened(session=summary))
