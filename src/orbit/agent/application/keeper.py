"""Autosaving the open session: once it's been used, it's saved after every change, so a crash or
ctrl+c loses nothing. A session nobody used is never written. The user can switch to another
saved session, or a new one, letting go of the one open, and open, rename or delete other saved
sessions, each under its lock.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from orbit.agent.application.failures import outcome
from orbit.agent.application.ports import Publish, SessionStore, StoredSession
from orbit.agent.application.storage_halt import Halting
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import LaneSnapshot
from orbit.agent.domain.sessions import (
    OPEN_UNDELETABLE,
    SessionRecord,
    Unreadable,
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
    viewable,
)
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    INVALID,
    LOCKED,
    SAVE_FAILED,
    UNKNOWN,
    ErrorOccurred,
    SessionSummary,
    Settings,
    TaskState,
    Usage,
)

Clock = Callable[[], datetime]
"""The time now, timezone-aware."""

IdMaker = Callable[[], str]
"""Makes a fresh, unique id."""

Forget = Callable[[str], None]
"""Has the logged events of the session whose id it's given deleted."""

CatchUp = Callable[[], Awaitable[None]]
"""Has the events published from now on counted on from the highest ``seq`` logged."""

Commit = Callable[[], None]
"""Switches what the session holds, unless that's refused now: then raises :class:`Refused`."""

Load = Callable[[SessionRecord], None]
"""Switches the session to hold what a saved record does, unless that's refused now: then raises
:class:`Refused`."""


@dataclass(frozen=True)
class _Action[T]:
    """What can be done to a saved session: its ``verb``, the ``check`` that makes of a session
    what it acts on, or the refusal saying why it can't be, and whether it ``reread``s the
    session under its lock."""

    verb: str
    check: Callable[[str, SessionRecord | Unreadable, bool], T | Refused]
    reread: bool = True


def _deletable(
    session_id: str, record: SessionRecord | Unreadable, locked: bool
) -> SessionRecord | Unreadable | Refused:
    """The saved session ``session_id``, as ``record``, to delete, or why it can't be."""
    return deletion_refusal(session_id, record, locked) or record


_OPEN = _Action("open", openable)
_RENAME = _Action("rename", renamable)
_DELETE = _Action("delete", _deletable, reread=False)
_VIEW = _Action("view", viewable, reread=False)


@dataclass(frozen=True)
class Left:
    """The session a switch left: its ``id``, if its lock is still held, to :meth:`leave`."""

    id: str | None


@dataclass(frozen=True)
class Opened:
    """A saved session opened: the session ``left`` for it, and the ``record`` it was opened
    from, as read under its lock."""

    left: Left
    record: SessionRecord


def local_now() -> datetime:
    """The time now, in the local timezone."""
    return datetime.now().astimezone()


def new_session_id() -> str:
    """A fresh, unique session id."""
    return uuid.uuid4().hex


def _kept(session_id: str) -> None:
    pass


async def _counting_on() -> None:
    pass


@dataclass(frozen=True)
class NewSessions:
    """How sessions are made and let go of: ``clock`` tells the time, ``new_id`` makes each
    one's id, ``forget`` deletes the logged events of one let go of unsaved, or deleted, and
    ``catch_up`` has the events of a saved one opened counted on past those another orbit
    logged of it."""

    clock: Clock = local_now
    new_id: IdMaker = new_session_id
    forget: Forget = _kept
    catch_up: CatchUp = _counting_on


_NEW_SESSIONS = NewSessions()


@dataclass(frozen=True)
class Contents:
    """What a saved session holds of the work: the ``settings`` its planner and new tasks use,
    the planner's Claude conversation, its ``tasks``, its ``reviews``' state, if any, and what
    its agents have ``spent`` in all."""

    settings: Settings
    planner_conversation: str | None
    tasks: tuple[TaskState, ...]
    reviews: LaneSnapshot | None
    spent: Usage


class SessionKeeper:
    """The open session, saved in ``store`` (if there is one); problems are reported with
    ``publish``. ``new_sessions`` tells the time and makes the session's id.

    It's ``used`` once it has a task or :meth:`use` is called, and only then saved, locked in
    the store from its first save until :meth:`close`, or until another session is opened in
    its place. A session closed or left unsaved, or deleted, has its logged events forgotten.

    A failing save halts the session through ``halt``, and nothing is saved while it's halted.
    """

    def __init__(
        self,
        store: SessionStore | None,
        publish: Publish,
        halt: Halting,
        new_sessions: NewSessions = _NEW_SESSIONS,
    ) -> None:
        self._store = store
        self._publish = publish
        self._halt = halt
        self._clock = new_sessions.clock
        self._new_id = new_sessions.new_id
        self._forget = new_sessions.forget
        self._catch_up = new_sessions.catch_up
        self.id = self._new_id()
        self.created = self._clock()
        self.name = default_name(self.created)
        self.used = False
        self._saved: Contents | None = None  # What the last save wrote.
        self._locked: bool | None = None  # Whether the lock was taken; None until tried.
        self._closed = False
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.

    def rename(self, name: str | None) -> None:
        """Name the session ``name``, unless it's none or blank; once used, it's saved again
        under that name."""
        self.name = (name or "").strip() or self.name
        self._saved = None

    @property
    def kept(self) -> bool:
        """Whether the session is saved, its lock held, so it stays saved once left."""
        return bool(self._locked)

    def use(self) -> None:
        """Mark the session used, so it's saved from now on."""
        self.used = True

    async def save(self, contents: Callable[[], Contents]) -> None:
        """Save the session, holding what ``contents`` says it does as it's saved, if it's used
        and changed since it was last saved."""
        error = await self._save(contents, retrying=False)
        if error is not None:
            self._halt.fail(FailedWrite.SESSION_RECORD, error)

    async def retry(self, contents: Callable[[], Contents]) -> RuntimeError | None:
        """Save the session as :meth:`save` does, though it's halted, as resuming does; returns
        the error when the save fails."""
        return await self._save(contents, retrying=True)

    async def _save(self, contents: Callable[[], Contents], retrying: bool) -> RuntimeError | None:
        """Save the session as :meth:`save` does, halted or not if ``retrying``; returns the
        error when the save fails."""
        if self._store is None:
            return None
        async with self._saving:
            contents_now = contents()
            if self._due(contents_now, retrying) and await self._lock(self._store):
                return await self._write(self._store, contents_now)
        return None

    def _due(self, contents: Contents, retrying: bool) -> bool:
        """Whether ``contents`` are to be saved: the session is open, not halted unless
        ``retrying``, used (as it is once it has a task) and holds something it didn't when it
        was last saved."""
        self.used = self.used or bool(contents.tasks)
        open_now = not self._closed and (retrying or not self._halt.halted)
        return open_now and self.used and contents != self._saved

    async def _lock(self, store: SessionStore) -> bool:
        """Lock the session in ``store`` on its first save; returns whether it's locked."""
        if self._locked is None:
            self._locked = await self._acquire(store)
        return self._locked

    async def _acquire(self, store: SessionStore) -> bool:
        """Take the session's lock in ``store``; reports it if it can't be taken."""
        acquired = await self._reported(store.acquire(self.id), "; the session won't be saved.")
        if acquired is None:
            return False
        if not acquired:
            message = f"Another orbit has the session {self.id} open, so it won't be saved."
            await self._publish(ErrorOccurred(code=LOCKED, message=message))
        return acquired

    async def _write(self, store: SessionStore, contents: Contents) -> RuntimeError | None:
        """Save ``contents`` as the session in ``store``, now its last active time; returns the
        error when it fails."""
        record = SessionRecord(
            id=self.id,
            name=self.name,
            created=self.created,
            last_active=self._clock(),
            settings=contents.settings,
            planner_conversation=contents.planner_conversation,
            tasks=contents.tasks,
            reviews=contents.reviews,
            spent=contents.spent,
        )
        error = await outcome(store.save(record))
        if isinstance(error, RuntimeError):
            return error
        self._saved = contents
        return None

    async def summaries(self) -> tuple[SessionSummary, ...]:
        """The saved sessions, the most recently active first, the one open here marked
        current; none without a store, or, reported, when they can't be listed."""
        stored = await self._stored()
        if stored is None:
            return ()
        return newest_first(
            stored_summary(s.id, s.record, s.id == self.id, s.locked) for s in stored
        )

    def summary(
        self, tasks: tuple[TaskState, ...], spent: Usage, last_active: datetime | None
    ) -> SessionSummary:
        """The session open here, holding ``tasks``, having ``spent`` so much and
        ``last_active`` then, as the list of sessions shows it."""
        summary = tasks_summary(self.id, self.name, tasks, spent)
        return summary.model_copy(update={"current": True, "last_active": last_active})

    async def saved(self, session_id: str) -> SessionRecord | None:
        """The saved session ``session_id`` as it was saved, to view; none, reported, if the
        sessions can't be listed. Raises :class:`Refused` if there's no such session or it's
        unreadable."""
        found = await self._found(session_id, _VIEW)
        return None if found is None else found[1]

    async def rename_saved(self, session_id: str, name: str) -> None:
        """Name the saved session ``session_id``, not the one open here, ``name``, holding its
        lock while it's rewritten. Refused (raising :class:`Refused`) if it can't be renamed;
        a failing save halts the session, and other store problems are reported."""
        found = await self._claimed(session_id, _RENAME)
        if found is None:
            return
        assert self._store is not None  # Only a store holds saved sessions.
        try:
            error = await outcome(self._store.save(found.model_copy(update={"name": name})))
            if isinstance(error, RuntimeError):
                self._halt.fail(FailedWrite.SESSION_RECORD, error)
        finally:
            await self._release(self._store, session_id)

    async def delete_saved(self, session_id: str) -> None:
        """Delete the saved session ``session_id``, lock and all, unless it's the one open here
        or another orbit has it open: then raises :class:`Refused`. Store problems are
        reported."""
        if session_id == self.id:
            raise Refused(INVALID, OPEN_UNDELETABLE)
        if await self._claimed(session_id, _DELETE) is not None:
            assert self._store is not None  # Only a store holds saved sessions.
            await self._delete(self._store, session_id)

    async def _delete(self, store: SessionStore, session_id: str) -> None:
        """Delete the session ``session_id`` from ``store``, whose lock this orbit took to, and
        then its events still to be logged; reports it, letting go of the lock, if it can't be
        deleted."""
        error = await outcome(store.delete(session_id))
        if isinstance(error, RuntimeError):
            await self._release(store, session_id)
            await self._publish(ErrorOccurred(code=SAVE_FAILED, message=str(error)))
            return
        self._forget(session_id)

    async def open(self, session_id: str, load: Load) -> Opened | None:
        """Make the saved session ``session_id`` the one open here, locked, if ``load`` switches
        to its record as read once it's locked; returns what was opened, or none if a store
        problem, reported, kept it from opening. A refusal, its own or ``load``'s, raises
        :class:`Refused`, with the session's lock let go of."""
        async with self._saving:
            record = await self._claimed(session_id, _OPEN)
            if record is None:
                return None
            assert self._store is not None  # Only saved sessions are locked to open.
            # Another orbit may have logged the session since this one opened: its events go
            # on from there, counted on before it's the session open and its events are logged.
            await self._catch_up()
            try:
                load(record)
            except Refused:
                await self._release(self._store, record.id)
                raise
            left = self._become(record.id, record.name, record.created)
            self._locked = self.used = True
        return Opened(left, record)

    async def _claimed[T](self, session_id: str, action: _Action[T]) -> T | None:
        """What ``action``'s check makes of the saved session ``session_id``, once its lock is
        taken, read again under the lock if ``action`` needs it so no work saved meanwhile is
        lost; none, reported, on a store problem. A refusal raises :class:`Refused`. Either way,
        any lock taken is let go of."""
        found = await self._found(session_id, action)
        if found is None:
            return None
        name, checked = found
        assert self._store is not None  # Only a store holds saved sessions.
        if not await self._claim(self._store, session_id, name, action.verb):
            return None
        if not action.reread:
            return checked
        current: T | None = None
        try:
            current = await self._reread(self._store, session_id, action)
        finally:
            if current is None:
                await self._release(self._store, session_id)
        return current

    async def _found[T](self, session_id: str, action: _Action[T]) -> tuple[str, T] | None:
        """The name of the saved session ``session_id`` and what ``action``'s check makes of it;
        none, reported, if the sessions can't be listed, and raises :class:`Refused` if there's
        no such session or it can't be ``action``ed."""
        stored = await self._stored()
        if stored is None:
            return None
        found = next((each for each in stored if each.id == session_id), None)
        if found is None:
            raise Refused(UNKNOWN, missing_message(session_id))
        checked = action.check(session_id, found.record, found.locked)
        if isinstance(checked, Refused):
            raise checked
        return record_name(session_id, found.record), checked

    async def _stored(self) -> list[StoredSession] | None:
        """The saved sessions, of which there are none without a store; ``None``, reported, if
        they can't be listed."""
        if self._store is None:
            return []
        return await self._reported(self._store.sessions())

    async def _reread[T](
        self, store: SessionStore, session_id: str, action: _Action[T]
    ) -> T | None:
        """What ``action``'s check makes of the session ``session_id`` as ``store`` holds it now,
        its lock taken; none, reported, if it can't be read, and raises :class:`Refused` if it
        can't be ``action``ed."""
        loaded = await self._reported(store.load(session_id))
        if loaded is None:
            return None
        checked = action.check(session_id, loaded, False)
        if isinstance(checked, Refused):
            raise checked
        return checked

    async def renew(self, name: str | None, commit: Commit) -> Left:
        """Make a new session ``name``d so (by the time, without a name) the one open here, if
        ``commit`` switches to it; returns the session left. A refused ``commit`` raises
        :class:`Refused`, leaving the session open as it was."""
        async with self._saving:
            commit()
            created = self._clock()
            named = (name or "").strip() or default_name(created)
            return self._become(self._new_id(), named, created)

    async def leave(self, left: Left, discard: bool) -> None:
        """Let go of the lock of the session ``left``, if it's held, or delete that session if
        ``discard``; reports it if it can't be let go of or deleted, letting go of the lock if
        it can't be deleted."""
        if left.id is None:
            return
        assert self._store is not None  # Only a store holds locks.
        if not discard:
            await self._release(self._store, left.id)
            return
        await self._delete(self._store, left.id)

    async def _claim(self, store: SessionStore, session_id: str, name: str, action: str) -> bool:
        """Take the lock of the session ``session_id``, ``name``d so, in ``store``, to
        ``action`` it; returns whether it did, reporting a store problem, and raises
        :class:`Refused` if another orbit holds it."""
        acquired = await self._reported(store.acquire(session_id))
        if acquired is None:
            return False
        if not acquired:
            raise Refused(LOCKED, locked_message(name, action))
        return True

    def _become(self, session_id: str, name: str, created: datetime) -> Left:
        """Be the session ``session_id``, unused and unsaved so far; returns the one left, whose
        logged events are forgotten if it was never saved."""
        left = Left(self.id if self._locked else None)
        if left.id is None:
            self._forget(self.id)
        self.id, self.name, self.created = session_id, name, created
        self.used, self._saved, self._locked = False, None, None
        return left

    async def close(self) -> None:
        """Stop saving, and let go of the session's lock if it was taken, else forget its logged
        events; reports it if the lock can't be let go of."""
        async with self._saving:
            self._closed = True
            if not self._locked:
                self._forget(self.id)
                return
            assert self._store is not None  # Only a store holds locks.
            await self._release(self._store, self.id)

    async def _release(self, store: SessionStore, session_id: str) -> None:
        """Let go of the session ``session_id``'s lock in ``store``; reports it if it can't be
        let go of."""
        await self._reported(store.release(session_id))

    async def _reported[T](self, call: Awaitable[T], suffix: str = "") -> T | None:
        """What ``call``, to the store, returns; none if it fails, reported with ``suffix``
        after the store's message."""
        result = await outcome(call)
        if isinstance(result, RuntimeError):
            await self._publish(ErrorOccurred(code=SAVE_FAILED, message=f"{result}{suffix}"))
            return None
        return result
