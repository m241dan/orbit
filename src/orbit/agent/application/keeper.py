"""Autosaving the open session: once it's been used, it's saved after every change, so a crash or
ctrl+c loses nothing. A session nobody used is never written. The user can switch to another
saved session, or a new one, letting go of the one open.

The repository's count of commits since the last super review is kept here too, though it
belongs to no one session.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from orbit.agent.application.ports import Publish, SessionStore
from orbit.agent.domain.reviews import LaneSnapshot
from orbit.agent.domain.sessions import (
    SessionRecord,
    default_name,
    locked_message,
    missing_message,
    newest_first,
    openable,
    stored_summary,
    tasks_summary,
)
from orbit.protocol import ErrorOccurred, SessionSummary, Settings, TaskState

Clock = Callable[[], datetime]
"""The time now, timezone-aware."""

IdMaker = Callable[[], str]
"""Makes a fresh, unique id."""

Commit = Callable[[], bool]
"""Switches what the session holds, unless that's refused now; returns whether it did."""


@dataclass(frozen=True)
class Left:
    """The session a switch left: its ``id``, if its lock is still held, to :meth:`leave`."""

    id: str | None


def local_now() -> datetime:
    """The time now, in the local timezone."""
    return datetime.now().astimezone()


def new_session_id() -> str:
    """A fresh, unique session id."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Contents:
    """What a saved session holds of the work: the ``settings`` its planner and new tasks use,
    the planner's Claude conversation, its ``tasks`` and its ``reviews``' state, if any."""

    settings: Settings
    planner_conversation: str | None
    tasks: tuple[TaskState, ...]
    reviews: LaneSnapshot | None


class SessionKeeper:
    """The open session, saved in ``store`` (if there is one); problems are reported with
    ``publish``. ``clock`` tells the time, and ``new_id`` makes the session's id.

    It's ``used`` once it has a task or :meth:`use` is called, and only then saved, locked in
    the store from its first save until :meth:`close`, or until another session is opened in
    its place.
    """

    def __init__(
        self,
        store: SessionStore | None,
        publish: Publish,
        clock: Clock = local_now,
        new_id: IdMaker = new_session_id,
    ) -> None:
        self._store = store
        self._publish = publish
        self._clock = clock
        self._new_id = new_id
        self.id = new_id()
        self.created = clock()
        self.name = default_name(self.created)
        self.used = False
        self._saved: Contents | None = None  # What the last save wrote.
        self._locked: bool | None = None  # Whether the lock was taken; None until tried.
        self._closed = False
        self._failing = False  # A save failed, and was reported, since the last that worked.
        self._count: int | None = None  # The super review count the store holds.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.

    def use(self) -> None:
        """Mark the session used, so it's saved from now on."""
        self.used = True

    async def save(self, contents: Callable[[], Contents]) -> None:
        """Save the session, holding what ``contents`` says it does as it's saved, if it's used
        and changed since it was last saved."""
        if self._store is None:
            return
        async with self._saving:
            contents_now = contents()
            if self._due(contents_now) and await self._lock(self._store):
                await self._write(self._store, contents_now)

    def _due(self, contents: Contents) -> bool:
        """Whether ``contents`` are to be saved: the session is open, used (as it is once it
        has a task) and holds something it didn't when it was last saved."""
        self.used = self.used or bool(contents.tasks)
        return not self._closed and self.used and contents != self._saved

    async def _lock(self, store: SessionStore) -> bool:
        """Lock the session in ``store`` on its first save; returns whether it's locked."""
        if self._locked is None:
            self._locked = await self._acquire(store)
        return self._locked

    async def _acquire(self, store: SessionStore) -> bool:
        """Take the session's lock in ``store``; reports it if it can't be taken."""
        try:
            if await store.acquire(self.id):
                return True
            message = f"Another orbit has the session {self.id} open, so it won't be saved."
        except RuntimeError as exc:
            message = f"{exc}; the session won't be saved."
        await self._publish(ErrorOccurred(message=message))
        return False

    async def _write(self, store: SessionStore, contents: Contents) -> None:
        """Save ``contents`` as the session in ``store``, now its last active time."""
        record = SessionRecord(
            id=self.id,
            name=self.name,
            created=self.created,
            last_active=self._clock(),
            settings=contents.settings,
            planner_conversation=contents.planner_conversation,
            tasks=contents.tasks,
            reviews=contents.reviews,
        )
        if await self._attempt(store.save(record)):
            self._saved = contents

    async def summaries(self) -> tuple[SessionSummary, ...]:
        """The saved sessions, the most recently active first, the one open here marked
        current; none without a store."""
        if self._store is None:
            return ()
        stored = await self._store.sessions()
        return newest_first(
            stored_summary(s.id, s.record, s.id == self.id, s.locked) for s in stored
        )

    def summary(self, tasks: tuple[TaskState, ...], last_active: datetime | None) -> SessionSummary:
        """The session open here, holding ``tasks`` and ``last_active`` then, as the list of
        sessions shows it."""
        summary = tasks_summary(self.id, self.name, tasks)
        return summary.model_copy(update={"current": True, "last_active": last_active})

    async def find(self, session_id: str) -> SessionRecord | str:
        """The record of the saved session ``session_id``, to open, or why it can't be."""
        stored = await self._store.sessions() if self._store else []
        for each in stored:
            if each.id == session_id:
                return openable(each.id, each.record, each.locked)
        return missing_message(session_id)

    async def open(self, record: SessionRecord, commit: Commit) -> Left | None:
        """Make the saved session ``record`` the one open here, locked, if ``commit`` switches
        to it once it's locked; returns the session left, or none if it didn't switch. Problems
        are reported."""
        assert self._store is not None  # Only saved sessions are found to open.
        async with self._saving:
            if not await self._claim(self._store, record):
                return None
            if not commit():
                await self._store.release(record.id)
                return None
            left = self._become(record.id, record.name, record.created)
            self._locked = self.used = True
        return left

    async def renew(self, name: str | None, commit: Commit) -> Left | None:
        """Make a new session ``name``d so (by the time, without a name) the one open here, if
        ``commit`` switches to it; returns the session left, or none if it didn't switch."""
        async with self._saving:
            if not commit():
                return None
            created = self._clock()
            named = (name or "").strip() or default_name(created)
            return self._become(self._new_id(), named, created)

    async def leave(self, left: Left, discard: bool) -> None:
        """Let go of the lock of the session ``left``, if it's held, or delete that session if
        ``discard``; reports it if it can't be deleted."""
        if left.id is None:
            return
        assert self._store is not None  # Only a store holds locks.
        if not discard:
            await self._store.release(left.id)
            return
        try:
            await self._store.delete(left.id)
        except RuntimeError as exc:
            await self._publish(ErrorOccurred(message=str(exc)))

    async def _claim(self, store: SessionStore, record: SessionRecord) -> bool:
        """Take ``record``'s lock in ``store``, to open it; reports it if it can't be taken."""
        try:
            if await store.acquire(record.id):
                return True
            message = locked_message(record.name)
        except RuntimeError as exc:
            message = str(exc)
        await self._publish(ErrorOccurred(message=message))
        return False

    def _become(self, session_id: str, name: str, created: datetime) -> Left:
        """Be the session ``session_id``, unused and unsaved so far; returns the one left."""
        left = Left(self.id if self._locked else None)
        self.id, self.name, self.created = session_id, name, created
        self.used, self._saved, self._locked, self._failing = False, None, None, False
        return left

    async def review_count(self) -> int | None:
        """The repository's commits since the last super review; none without a store."""
        if self._store is None:
            return None
        self._count = await self._store.review_count()
        return self._count

    async def save_review_count(self, count: int) -> None:
        """Save ``count`` as the repository's commits since the last super review, if it
        changed."""
        if self._store is None or count == self._count:
            return
        if await self._attempt(self._store.save_review_count(count)):
            self._count = count

    async def _attempt(self, saving: Awaitable[None]) -> bool:
        """Await ``saving``; returns whether it worked. The first failure since the last save
        that worked is reported, and work carries on."""
        try:
            await saving
        except RuntimeError as exc:
            if not self._failing:
                self._failing = True
                await self._publish(ErrorOccurred(message=str(exc)))
            return False
        self._failing = False
        return True

    async def close(self) -> None:
        """Stop saving, and let go of the session's lock if it was taken."""
        async with self._saving:
            self._closed = True
            if self._store is not None and self._locked:
                await self._store.release(self.id)
