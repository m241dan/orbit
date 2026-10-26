"""The copies agents whose role works in a copy work in: a fresh one for each task run, removed
after it, and one for each conversational role's conversation, made at its first turn, kept in
the session across turns, switches and restarts, and removed on a reset or once its role's copy
TTL has passed since the conversation's last turn."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from orbit.agent.application.ports import (
    CoderTree,
    Copies,
    Publish,
    ReadFailed,
    SessionStore,
    TreeFailed,
)
from orbit.agent.domain.copies import expired_roles, unrecorded
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SavedCopy, SessionRecord, Unreadable
from orbit.protocol import GIT_FAILED, ErrorOccurred

CHECK_EVERY = 3600.0
"""How often, in seconds, the copies are checked for removal while orbit runs."""

Clock = Callable[[], datetime]
"""What tells the time."""

Recorded = Mapping[str, SavedCopy]
"""A session's copies, by role."""


class TreeCopies:
    """Copies of ``tree``'s base commit, made and removed by the tree."""

    def __init__(self, tree: CoderTree) -> None:
        self._tree = tree

    async def make(self) -> str:
        return await self._tree.copy(await self._tree.base_commit())

    async def remove(self, path: str) -> None:
        await self._tree.drop_copy(path)

    async def leftovers(self) -> list[str]:
        return await self._tree.copies()


@dataclass(frozen=True)
class OtherCopies:
    """The copies recorded in the other saved sessions that could be read, and whether every
    session could be, so that no copy unaccounted for is recorded in one."""

    recorded: tuple[Recorded, ...] = ()
    complete: bool = True


async def saved_copies(store: SessionStore | None, open_id: str) -> OtherCopies:
    """The copies recorded in the sessions saved in ``store`` but the open one, ``open_id``:
    none, completely, without a store, as there are no others; none, incompletely, when the
    sessions can't be listed; and those of the readable ones, incompletely if any can't be."""
    if store is None:
        return OtherCopies()
    try:
        stored = await store.sessions()
    except ReadFailed:
        return OtherCopies(complete=False)
    return _copies_of([saved.record for saved in stored if saved.id != open_id])


def _copies_of(others: Sequence[SessionRecord | Unreadable]) -> OtherCopies:
    """The copies recorded in ``others``, complete if every one of them could be read."""
    records = [record for record in others if isinstance(record, SessionRecord)]
    return OtherCopies(tuple(record.copies for record in records), len(records) == len(others))


def copy_ttls(roles: Mapping[str, Role]) -> dict[str, float]:
    """How long each of ``roles`` keeps its copy, in hours, by role."""
    return {name: role.copy_ttl_hours for name, role in roles.items()}


@asynccontextmanager
async def fresh_copy(copies: Copies, publish: Publish) -> AsyncIterator[str]:
    """A new copy from ``copies``, removed on the way out, however the body ends.

    Making it raises :class:`TreeFailed` as ``copies`` does. A failure to remove it is published
    with ``publish`` rather than raised, so the body's result, or its exception, stands.
    """
    path = await copies.make()
    try:
        yield path
    finally:
        await removed(copies, path, publish)


async def removed(copies: Copies, path: str, publish: Publish) -> None:
    """Remove the copy at ``path`` from ``copies``, publishing why with ``publish`` if it can't
    be."""
    try:
        await copies.remove(path)
    except TreeFailed as exc:
        message = f"The copy at {path} couldn't be removed and is left over: {exc}"
        await publish(ErrorOccurred(code=GIT_FAILED, message=message))


async def _nothing_changed() -> None:
    """Save nothing."""


class RoleCopies:
    """The copies the open session's conversational roles work in, by role, made by ``copies``,
    their turns timed by ``clock``; failures to remove one are published with ``publish``, and
    :attr:`changed` is awaited once copies are removed by age, to save the session."""

    def __init__(self, copies: Copies, clock: Clock, publish: Publish) -> None:
        self._copies = copies
        self._clock = clock
        self._publish = publish
        self._held: dict[str, SavedCopy] = {}
        self._made: set[str] = set()  # The copies made in this run, which aren't leftovers.
        self._turns: dict[str, str] = {}  # The copies of the roles taking a turn, by role.
        self.changed: Callable[[], Awaitable[None]] = _nothing_changed

    @property
    def held(self) -> dict[str, SavedCopy]:
        """The session's copies, by role, to save."""
        return dict(self._held)

    def replace(self, held: Recorded) -> None:
        """Hold ``held``, the copies of a session switched to, in place of the session left's,
        which stay where they are, recorded in it."""
        self._held = dict(held)

    async def enter(self, role: str) -> str:
        """The copy role ``role``'s conversation takes a turn in now: the one it has, unless it
        was removed meanwhile, else a new one, kept from expiring until :meth:`leave`; raises
        :class:`TreeFailed` as making one does."""
        saved = self._held.get(role)
        path = saved.path if saved and await self._exists(saved.path) else await self._make()
        self._held[role] = SavedCopy(path=path, last_turn=self._clock())
        self._turns[role] = path
        return path

    def leave(self, role: str) -> None:
        """End role ``role``'s turn, timing its copy's TTL from now, if it still holds the copy
        the turn was taken in."""
        path = self._turns.pop(role, None)
        saved = self._held.get(role)
        if saved is not None and saved.path == path:
            self._held[role] = SavedCopy(path=path, last_turn=self._clock())

    async def drop(self, role: str) -> None:
        """Remove role ``role``'s copy, if it has one, with its conversation."""
        saved = self._held.pop(role, None)
        if saved is not None:
            await self._remove(saved.path)

    async def expire(self, ttls: Mapping[str, float]) -> None:
        """Remove the copies whose role's TTL in ``ttls``, in hours, has passed since their
        conversation's last turn, but those of roles taking a turn, saving the session if any
        were."""
        idle = {role: saved for role, saved in self._held.items() if role not in self._turns}
        roles = expired_roles(idle, ttls, self._clock())
        for role in roles:
            await self._remove(self._held.pop(role).path)
        if roles:
            await self.changed()

    async def sweep(self, others: OtherCopies, ttls: Mapping[str, float]) -> None:
        """Remove the copies past their TTL in ``ttls``: this session's, and those recorded in
        the ``others`` sessions that are still there, which keep their records until they next take
        a turn; then
        those left over that no session records, if every session's records were read."""
        await self.expire(ttls)
        leftovers = await self._leftovers()
        await self._expire_others(others.recorded, ttls, {*leftovers, *self._made})
        if not others.complete:
            return
        for path in unrecorded(leftovers, [self._held, *others.recorded]):
            await self._remove(path)

    async def _expire_others(
        self, others: Sequence[Recorded], ttls: Mapping[str, float], present: set[str]
    ) -> None:
        """Remove the copies recorded in ``others`` past their TTL in ``ttls`` that are still
        ``present``: one already removed, as an earlier sweep does, is left alone."""
        now = self._clock()
        for recorded in others:
            for role in expired_roles(recorded, ttls, now):
                if recorded[role].path in present:
                    await self._remove(recorded[role].path)

    async def keep(
        self,
        others: Callable[[], Awaitable[OtherCopies]],
        ttls: Callable[[], Mapping[str, float]],
        every: float = CHECK_EVERY,
    ) -> None:
        """Sweep the copies, as :meth:`sweep` does, with the copies of the ``others`` sessions
        and the ``ttls`` now, then remove this session's past their TTL ``every`` so many
        seconds, until cancelled."""
        await self.sweep(await others(), ttls())
        while True:
            await asyncio.sleep(every)
            await self.expire(ttls())

    async def _make(self) -> str:
        path = await self._copies.make()
        self._made.add(path)
        return path

    async def _exists(self, path: str) -> bool:
        """Whether the copy at ``path`` is still there: made in this run, or left by another."""
        return path in self._made or path in await self._leftovers()

    async def _leftovers(self) -> list[str]:
        """The copies there are that this run didn't make; none when they can't be listed,
        publishing why."""
        try:
            return await self._copies.leftovers()
        except TreeFailed as exc:
            message = f"Couldn't list the copies: {exc}"
            await self._publish(ErrorOccurred(code=GIT_FAILED, message=message))
            return []

    async def _remove(self, path: str) -> None:
        await removed(self._copies, path, self._publish)
        self._made.discard(path)
