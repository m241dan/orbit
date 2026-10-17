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
from typing import Literal

from orbit.agent.application.ports import Publish, SessionStore, StoredSession
from orbit.agent.domain.reviews import LaneSnapshot
from orbit.agent.domain.sessions import (
    OPEN_UNDELETABLE,
    SessionRecord,
    default_name,
    deletion_refusal,
    locked_message,
    missing_message,
    newest_first,
    openable,
    record_name,
    renamable,
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

SaveKind = Literal["session", "count"]
"""What a save writes: the session, or the super review count."""


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

    A failing save is reported once, until a save of the same kind (the session, or the super
    review count) works again.
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
        # The kinds of save that failed, and were reported, since the last of each that worked.
        self._failing: set[SaveKind] = set()
        self._count: int | None = None  # The super review count the store holds.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.
        self._counting = asyncio.Lock()  # Likewise for count saves.

    def rename(self, name: str | None) -> None:
        """Name the session ``name``, unless it's none or blank; once used, it's saved again
        under that name."""
        self.name = (name or "").strip() or self.name
        self._saved = None

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
        if await self._attempt("session", store.save(record)):
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
        stored = await self._stored(session_id)
        if stored is None:
            return missing_message(session_id)
        return openable(stored.id, stored.record, stored.locked)

    async def _stored(self, session_id: str) -> StoredSession | None:
        """The saved session ``session_id``; none if there's no such session."""
        stored = await self._store.sessions() if self._store else []
        return next((each for each in stored if each.id == session_id), None)

    async def rename_saved(self, session_id: str, name: str) -> None:
        """Name the saved session ``session_id``, not the one open here, ``name``, holding its
        lock while it's rewritten. Problems are reported."""
        stored = await self._stored(session_id)
        if stored is None:
            found: SessionRecord | str = missing_message(session_id)
        else:
            found = renamable(stored.id, stored.record, stored.locked)
        if isinstance(found, str):
            await self._publish(ErrorOccurred(message=found))
            return
        assert self._store is not None  # Only a store holds saved sessions.
        if await self._claim(self._store, found.id, found.name, "rename"):
            await self._rewrite(self._store, found.model_copy(update={"name": name}))

    async def _rewrite(self, store: SessionStore, record: SessionRecord) -> None:
        """Save ``record`` in ``store``, whose lock this orbit took to, then let go of the lock;
        reports it if it can't be saved."""
        try:
            await store.save(record)
        except RuntimeError as exc:
            await self._publish(ErrorOccurred(message=str(exc)))
        finally:
            await store.release(record.id)

    async def delete_saved(self, session_id: str) -> None:
        """Delete the saved session ``session_id``, lock and all, unless it's the one open here
        or another orbit has it open. Problems are reported."""
        stored = await self._stored(session_id)
        refusal = self._deletion_refusal(session_id, stored)
        if refusal is not None:
            await self._publish(ErrorOccurred(message=refusal))
            return
        assert self._store is not None and stored is not None  # Found in the store.
        name = record_name(session_id, stored.record)
        if await self._claim(self._store, session_id, name, "delete"):
            await self._delete(self._store, session_id)

    def _deletion_refusal(self, session_id: str, stored: StoredSession | None) -> str | None:
        """Why the session ``session_id``, saved as ``stored``, can't be deleted, if it can't."""
        if session_id == self.id:
            return OPEN_UNDELETABLE
        if stored is None:
            return missing_message(session_id)
        return deletion_refusal(session_id, stored.record, stored.locked)

    async def _delete(self, store: SessionStore, session_id: str) -> None:
        """Delete the session ``session_id`` from ``store``, whose lock this orbit took to;
        reports it, letting go of the lock, if it can't be deleted."""
        try:
            await store.delete(session_id)
        except RuntimeError as exc:
            await store.release(session_id)
            await self._publish(ErrorOccurred(message=str(exc)))

    async def open(self, record: SessionRecord, commit: Commit) -> Left | None:
        """Make the saved session ``record`` the one open here, locked, if ``commit`` switches
        to it once it's locked; returns the session left, or none if it didn't switch. Problems
        are reported."""
        assert self._store is not None  # Only saved sessions are found to open.
        async with self._saving:
            if not await self._claim(self._store, record.id, record.name):
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

    async def _claim(
        self, store: SessionStore, session_id: str, name: str, action: str = "open"
    ) -> bool:
        """Take the lock of the session ``session_id``, ``name``d so, in ``store``, to
        ``action`` it; reports it if it can't be taken."""
        try:
            if await store.acquire(session_id):
                return True
            message = locked_message(name, action)
        except RuntimeError as exc:
            message = str(exc)
        await self._publish(ErrorOccurred(message=message))
        return False

    def _become(self, session_id: str, name: str, created: datetime) -> Left:
        """Be the session ``session_id``, unused and unsaved so far; returns the one left."""
        left = Left(self.id if self._locked else None)
        self.id, self.name, self.created = session_id, name, created
        self.used, self._saved, self._locked = False, None, None
        self._failing.discard("session")
        return left

    async def review_count(self) -> int | None:
        """The repository's commits since the last super review; none without a store."""
        if self._store is None:
            return None
        self._count = await self._store.review_count()
        return self._count

    async def save_review_count(self, count: int) -> None:
        """Save ``count`` as the repository's commits since the last super review, if it
        changed. Count saves are made one at a time, so the newest is written last."""
        if self._store is None:
            return
        async with self._counting:
            if count != self._count and await self._attempt(
                "count", self._store.save_review_count(count)
            ):
                self._count = count

    async def _attempt(self, kind: SaveKind, saving: Awaitable[None]) -> bool:
        """Await ``saving``, a save of ``kind``; returns whether it worked. The first failure
        since the last save of that kind that worked is reported, and work carries on."""
        try:
            await saving
        except RuntimeError as exc:
            if kind not in self._failing:
                self._failing.add(kind)
                await self._publish(ErrorOccurred(message=str(exc)))
            return False
        self._failing.discard(kind)
        return True

    async def close(self) -> None:
        """Stop saving, and let go of the session's lock if it was taken; reports it if the lock
        can't be let go of."""
        async with self._saving:
            self._closed = True
            if self._store is None or not self._locked:
                return
            try:
                await self._store.release(self.id)
            except RuntimeError as exc:
                await self._publish(ErrorOccurred(message=str(exc)))
