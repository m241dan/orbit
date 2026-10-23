"""The repository's count of commits since the last super review, kept apart from any one
session."""

import asyncio

from orbit.agent.application.failures import ReportedOnce, outcome
from orbit.agent.application.ports import Publish, SessionStore, StoreFailed
from orbit.agent.application.storage_halt import Halting
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.protocol import INTERNAL


class ReviewCount:
    """The repository's super review count, saved in ``store`` (if there is one); a failing load
    is reported with ``publish``, and a failing save halts the session through ``halt``.
    Nothing is saved while it's halted, nor until a load has worked, so a count never read
    can't replace the one saved."""

    def __init__(self, store: SessionStore | None, halt: Halting, publish: Publish) -> None:
        self._store = store
        self._halt = halt
        self._loads = ReportedOnce(publish, INTERNAL)
        self._count: int | None = None  # The count the store holds; none until it's read.
        self._wanted: int | None = None  # The count last asked to be saved.
        self._saving = asyncio.Lock()  # Saves one at a time, so the newest is written last.
        self._loading = asyncio.Lock()  # Reads it once at a time, a recount's last.

    async def count_on(self, planner: ReviewPlanner) -> None:
        """Have ``planner`` count on from the count saved, unless that's been read already,
        can't be read, or ``planner`` counted on itself while it was: its own count is never
        replaced otherwise, so it isn't rolled back by a count read as it's saved."""
        async with self._loading:
            if self._count is None:
                await self._count_on(planner)

    async def recount_on(self, planner: ReviewPlanner) -> None:
        """Have ``planner`` count on from the count saved, read again, as another orbit may
        have counted on since it was read, unless it can't be read or ``planner`` counted on
        itself while it was."""
        async with self._loading:
            self.forget()
            await self._count_on(planner)

    async def _count_on(self, planner: ReviewPlanner) -> None:
        """Have ``planner`` count on from the count saved, unless that can't be read or
        ``planner`` counted on itself while it was."""
        before = planner.since_super
        count = await self.load()
        if count is not None and planner.since_super == before:
            planner.since_super = count

    async def load(self) -> int | None:
        """The repository's commits since the last super review; none without a store, or when
        it can't be read, the first such failure since a load worked reported."""
        if self._store is None:
            return None
        if not await self._loads.attempt(self._read(self._store)):
            return None
        return self._count

    async def _read(self, store: SessionStore) -> None:
        """Take the count ``store`` holds as the one saved."""
        self._count = await store.review_count()

    def forget(self) -> None:
        """Forget the count read, as another orbit may have saved one since; nothing is saved
        until a load works again."""
        self._count = None

    async def save(self, count: int) -> None:
        """Save ``count`` as the repository's commits since the last super review, if it
        changed."""
        if self._store is None:
            return
        self._wanted = count
        async with self._saving:
            if self._halt.halted:
                return
            error = await self._write(self._store, count)
            if error is not None:
                self._halt.fail(FailedWrite.REVIEW_COUNT, error)

    async def retry(self) -> StoreFailed | None:
        """Save the count last asked to be saved, if it changed, though the session is halted,
        as resuming does; returns the error when the save fails."""
        if self._store is None or self._wanted is None:
            return None
        async with self._saving:
            return await self._write(self._store, self._wanted)

    async def _write(self, store: SessionStore, count: int) -> StoreFailed | None:
        """Save ``count`` in ``store``, if it changed and the count saved has been read; returns
        the error when it fails."""
        if self._count is None or count == self._count:
            return None
        error = await outcome(store.save_review_count(count))
        if isinstance(error, StoreFailed):
            return error
        self._count = count
        return None
