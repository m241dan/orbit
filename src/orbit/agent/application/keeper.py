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

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.ports import (
    LockLeft,
    Publish,
    SessionStore,
    StoredSession,
    StoreFailed,
)
from orbit.agent.application.storage_halt import Halting, written
from orbit.agent.domain.refusals import STOPPING, Refused, reason
from orbit.agent.domain.reviews import LaneSnapshot
from orbit.agent.domain.sessions import (
    OPEN_UNDELETABLE,
    OPEN_UNRENAMABLE,
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
    session_name,
    stored_summary,
    tasks_summary,
    viewable,
)
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import (
    INTERNAL,
    INVALID,
    LOCKED,
    SAVE_FAILED,
    UNKNOWN,
    ErrorOccurred,
    Notice,
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
"""Has the events published from now on counted on from the highest ``seq`` logged; raises
:class:`ReadFailed` when that can't be read."""

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
class _Renaming:
    """A rename of the saved session ``session_id`` to ``name`` whose save failed, to retry."""

    session_id: str
    name: str


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


@dataclass(frozen=True)
class AlreadyOpen:
    """A saved session asked to open that was the one open already, so nothing was opened."""


ALREADY_OPEN = AlreadyOpen()
"""The session asked to open was the one open already."""


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
    the planner's Claude conversation, its ``tasks``, its ``reviews``' state, if any, what its
    agents have ``spent`` in all, and the ``planner_notes`` its planner is still to hear."""

    settings: Settings
    planner_conversation: str | None
    tasks: tuple[TaskState, ...]
    reviews: LaneSnapshot | None
    spent: Usage
    planner_notes: tuple[str, ...] = ()


class SessionKeeper:
    """The open session, saved in ``store`` (if there is one); problems are reported with
    ``publish``. ``new_sessions`` tells the time and makes the session's id.

    It's ``used`` once it has a task or :meth:`use` is called, and only then saved, locked in
    the store from its first save until :meth:`close`, or until another session is opened in
    its place. A session closed or left unsaved, or deleted, has its logged events forgotten.

    A failing save, or a store failing as the lock is taken, halts the session through ``halt``,
    and nothing is saved while it's halted; :meth:`retry` takes the lock and saves it again, along
    with a saved session's rename whose save failed.
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
        self._listings = ReportedOnce(publish, INTERNAL)
        self._halt = halt
        self._clock = new_sessions.clock
        self._new_id = new_sessions.new_id
        self._forget = new_sessions.forget
        self._catch_up = new_sessions.catch_up
        self.id = self._new_id()
        self.created = self._clock()
        self.name = default_name(self.created)
        self.used = False
        self._saved: tuple[str, Contents] | None = None  # The name and contents last saved.
        self._locked: bool | None = None  # Whether the lock was taken; None until tried.
        self._closed = False
        self._renaming: _Renaming | None = None  # The rename whose save failed, to retry.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.

    def rename(self, name: str | None) -> None:
        """Name the session ``name``, unless it's none or blank; once used, it's saved again
        under that name."""
        self.name = session_name(name) or self.name

    @property
    def kept(self) -> bool:
        """Whether the session is saved, its lock held, so it stays saved once left."""
        return bool(self._locked)

    def use(self) -> None:
        """Mark the session used, so it's saved from now on."""
        self.used = True

    async def save(self, contents: Callable[[], Contents]) -> None:
        """Save the session, holding what ``contents`` says it does as it's saved, if it's used
        and changed since it was last saved. A failing save, or a store failing as the session's
        lock is taken on its first save, halts the session."""
        if self._store is None:
            return
        store = self._store
        async with self._saving:
            held = self._held(contents)
            await written(self._halt, FailedWrite.SESSION_RECORD, lambda: self._save(store, held))

    async def retry(self, contents: Callable[[], Contents]) -> StoreFailed | None:
        """Save the session as :meth:`save` does, though it's halted, as resuming does, and then
        redo the rename of a saved session whose save failed; returns the error when a save
        fails, or when a store problem keeps the rename from being redone, keeping it to retry.
        A rename whose session can't be claimed any more is dropped, reported."""
        error = await self._resave(contents)
        if error is not None or self._renaming is None:
            return error
        renaming, self._renaming = self._renaming, None
        try:
            renamed = await self._rename(renaming, resuming=True)
        except Refused as refusal:
            await self._publish(ErrorOccurred(code=refusal.code, message=reason(refusal)))
            return None
        if renamed is False:
            self._renaming = renaming
            return StoreFailed(
                f"Couldn't claim the saved session {renaming.session_id} to rename it."
            )
        return renamed if isinstance(renamed, StoreFailed) else None

    async def _resave(self, contents: Callable[[], Contents]) -> StoreFailed | None:
        """Save the session as :meth:`save` does, though it's halted; returns the error when the
        save fails."""
        if self._store is None:
            return None
        async with self._saving:
            return await self._save(self._store, self._held(contents))

    def _held(self, contents: Callable[[], Contents]) -> Contents:
        """What ``contents`` says the session holds now, marking it used once it has a task."""
        held = contents()
        self.used = self.used or bool(held.tasks)
        return held

    async def _save(self, store: SessionStore, contents: Contents) -> StoreFailed | None:
        """Save ``contents`` in ``store``, holding the lock to save, if they're due, taking the
        session's lock on its first save; returns the error when the save, or taking the lock,
        fails."""
        if not self._due(contents):
            return None
        locked = await self._lock(store)
        if locked is True:
            return await self._write(store, contents)
        return locked if isinstance(locked, StoreFailed) else None

    def _due(self, contents: Contents) -> bool:
        """Whether ``contents`` are to be saved: the session is open, used (as it is once it has
        a task) and holds something it didn't when it was last saved, its name included."""
        return not self._closed and self.used and (self.name, contents) != self._saved

    async def _lock(self, store: SessionStore) -> StoreFailed | bool:
        """Lock the session in ``store`` on its first save; returns whether it's locked, or the
        error when the store fails as it's taken, leaving it to be tried again."""
        if self._locked is None:
            acquired = await self._acquire(store)
            if isinstance(acquired, StoreFailed):
                return acquired
            self._locked = acquired
        return self._locked

    async def _acquire(self, store: SessionStore) -> StoreFailed | bool:
        """Take the session's lock in ``store``; returns whether it was taken, reporting it if
        another orbit holds it, or the error when the store fails."""
        acquired = await outcome(store.acquire(self.id))
        if acquired is False:
            message = f"Another orbit has the session {self.id} open, so it won't be saved."
            await self._publish(ErrorOccurred(code=LOCKED, message=message))
        return acquired

    async def _write(self, store: SessionStore, contents: Contents) -> StoreFailed | None:
        """Save ``contents`` as the session in ``store``, now its last active time; returns the
        error when it fails."""
        record = SessionRecord(
            id=self.id,
            name=self.name,
            created=self.created,
            last_active=self._clock(),
            settings=contents.settings,
            planner_conversation=contents.planner_conversation,
            planner_notes=contents.planner_notes,
            tasks=contents.tasks,
            reviews=contents.reviews,
            spent=contents.spent,
        )
        error = await outcome(store.save(record))
        if isinstance(error, StoreFailed):
            return error
        self._saved = (record.name, contents)
        return None

    async def summaries(self) -> tuple[SessionSummary, ...]:
        """The saved sessions, the most recently active first, the one open here marked
        current; none without a store, or when they can't be listed, the first such failure
        since a listing worked reported."""
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

    async def rename_saved(self, session_id: str, name: str) -> bool:
        """Name the saved session ``session_id``, not the one open here, ``name``, holding its
        lock while it's rewritten; returns whether it was renamed. Refused (raising
        :class:`Refused`) if it can't be renamed; a failing save halts the session, and other
        store problems are reported."""
        renamed = await self._rename(_Renaming(session_id, name), resuming=False)
        if isinstance(renamed, StoreFailed):
            self._halt.fail(FailedWrite.SESSION_RECORD, renamed)
            return False
        return renamed

    async def _rename(self, renaming: _Renaming, resuming: bool) -> StoreFailed | bool:
        """Carry out ``renaming`` as :meth:`rename_saved` does; returns whether it was renamed,
        not if a store problem kept it from being claimed, or the error when its save fails,
        keeping it to :meth:`retry`. Refused if it's the session open here by then, or if it's
        halted by then, unless ``resuming``."""
        async with self._saving:
            # A save may have halted the session while this one waited.
            if not resuming:
                self._halt.check()
            # A switch may have opened it while this one waited.
            if renaming.session_id == self.id:
                raise Refused(INVALID, OPEN_UNRENAMABLE)
            found = await self._claimed(renaming.session_id, _RENAME)
            if found is None:
                return False
            assert self._store is not None  # Only a store holds saved sessions.
            try:
                error = await outcome(
                    self._store.save(found.model_copy(update={"name": renaming.name}))
                )
            finally:
                await self._release(self._store, renaming.session_id)
        if not isinstance(error, StoreFailed):
            return True
        self._renaming = renaming
        return error

    async def delete_saved(self, session_id: str) -> bool:
        """Delete the saved session ``session_id``, lock and all, unless it's the one open here
        or another orbit has it open: then raises :class:`Refused`; returns whether it was
        deleted, refused too while the session is halted. Store problems are reported."""
        async with self._saving:
            # A save may have halted the session while this one waited.
            self._halt.check()
            # A switch may have opened it while this one waited.
            if session_id == self.id:
                raise Refused(INVALID, OPEN_UNDELETABLE)
            if await self._claimed(session_id, _DELETE) is None:
                return False
            assert self._store is not None  # Only a store holds saved sessions.
            return await self._delete(self._store, session_id)

    async def _delete(self, store: SessionStore, session_id: str) -> bool:
        """Delete the session ``session_id`` from ``store``, whose lock this orbit took to, and
        then its events still to be logged; returns whether it was deleted, reporting it and
        letting go of the lock if it can't be. A lock file left behind doesn't undo the deletion:
        it's noted."""
        error = await outcome(store.delete(session_id))
        if isinstance(error, LockLeft):
            await self._publish(Notice(message=str(error)))
        elif isinstance(error, StoreFailed):
            await self._release(store, session_id)
            await self._publish(ErrorOccurred(code=SAVE_FAILED, message=str(error)))
            return False
        self._forget(session_id)
        return True

    async def open(self, session_id: str, load: Load) -> Opened | AlreadyOpen | None:
        """Make the saved session ``session_id`` the one open here, locked, if ``load`` switches
        to its record as read once it's locked; returns what was opened, :data:`ALREADY_OPEN`
        if it's the one open already, or none if a store problem, reported, kept it from
        opening. A refusal, its own or ``load``'s, raises :class:`Refused`, with the session's
        lock let go of, as it is if catching up fails or is cancelled; refused, with no lock
        taken, once closed or while halted."""
        async with self._saving:
            self._check_open()
            # Another switch may have opened it while this one waited.
            if session_id == self.id:
                return ALREADY_OPEN
            record = await self._claimed(session_id, _OPEN)
            if record is None:
                return None
            assert self._store is not None  # Only saved sessions are locked to open.
            # Another orbit may have logged the session since this one opened: its events go
            # on from there, counted on before it's the session open and its events are logged.
            try:
                await self._catch_up()
                load(record)
            except BaseException:
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
        try:
            return await self._reread(self._store, session_id, action)
        except BaseException:
            await self._release(self._store, session_id)
            raise

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
        """The saved sessions, of which there are none without a store; ``None`` if they can't be
        listed, the first such failure since a listing worked reported, so one action listing
        them several times reports it once."""
        if self._store is None:
            return []
        return await self._listings.value(self._store.sessions())

    async def _reread[T](self, store: SessionStore, session_id: str, action: _Action[T]) -> T:
        """What ``action``'s check makes of the session ``session_id`` as ``store`` holds it now,
        its lock taken; raises :class:`Refused` if it can't be ``action``ed, unreadable
        included."""
        checked = action.check(session_id, await store.load(session_id), False)
        if isinstance(checked, Refused):
            raise checked
        return checked

    async def renew(self, name: str | None, commit: Commit) -> Left:
        """Make a new session ``name``d so (by the time, without a name) the one open here, if
        ``commit`` switches to it; returns the session left. A refused ``commit`` raises
        :class:`Refused`, leaving the session open as it was, as is a renewal once closed or
        while halted."""
        async with self._saving:
            self._check_open()
            commit()
            created = self._clock()
            named = session_name(name) or default_name(created)
            return self._become(self._new_id(), named, created)

    def _check_open(self) -> None:
        """Refuse what would make a session the one open here once this is closed, as nothing
        would let go of its lock, or while it's halted, as a save may have halted it while this
        waited, and the work it holds isn't saved."""
        if self._closed:
            raise Refused(INVALID, STOPPING)
        self._halt.check()

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
        logged events are forgotten if it was never saved, unless it's halted, so they're kept
        to resume."""
        left = Left(self.id if self._locked else None)
        if left.id is None and not self._halt.halted:
            self._forget(self.id)
        self.id, self.name, self.created = session_id, name, created
        self.used, self._saved, self._locked = False, None, None
        return left

    def retried(self, contents: Callable[[], Contents]) -> RecordRetry:
        """The session record's write, which resuming retries as :meth:`retry` does, holding
        what ``contents`` says."""
        return RecordRetry(self, contents)

    async def close(self) -> None:
        """Stop saving, and let go of the session's lock if it was taken, else forget its logged
        events unless it's halted; reports it if the lock can't be let go of."""
        async with self._saving:
            self._closed = True
            if not self._locked:
                if not self._halt.halted:
                    self._forget(self.id)
                return
            assert self._store is not None  # Only a store holds locks.
            await self._release(self._store, self.id)

    async def _release(self, store: SessionStore, session_id: str) -> None:
        """Let go of the session ``session_id``'s lock in ``store``; reports it if it can't be
        let go of."""
        await self._reported(store.release(session_id))

    async def _reported[T](self, call: Awaitable[T]) -> T | None:
        """What ``call``, to the store, returns; none if it fails, reported."""
        result = await outcome(call)
        if isinstance(result, StoreFailed):
            await self._publish(ErrorOccurred(code=SAVE_FAILED, message=str(result)))
            return None
        return result


@dataclass(frozen=True)
class RecordRetry:
    """The session record's write, retried by ``keeper`` holding what ``contents`` says."""

    keeper: SessionKeeper
    contents: Callable[[], Contents]

    @property
    def write(self) -> FailedWrite:
        """The session record's write."""
        return FailedWrite.SESSION_RECORD

    async def retry(self) -> StoreFailed | None:
        """Save the session record, and redo a failed rename, though halted; returns the error
        when that fails."""
        return await self.keeper.retry(self.contents)
