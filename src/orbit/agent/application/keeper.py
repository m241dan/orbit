"""Autosaving the open session: once it's been used, it's saved after every change, so a crash or
ctrl+c loses nothing. A session nobody used is never written.

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
from orbit.agent.domain.sessions import SessionRecord, default_name
from orbit.protocol import ErrorOccurred, Settings, TaskState

Clock = Callable[[], datetime]
"""The time now, timezone-aware."""

IdMaker = Callable[[], str]
"""Makes a fresh, unique id."""


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
    the store from its first save until :meth:`close`.
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
