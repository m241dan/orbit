"""The copies agents whose role works in a copy work in: a fresh one for each task run, removed
after it, and one for each conversational role's conversation, made at its first turn, kept in
the session across turns, switches and restarts, and removed on a reset or once its role's copy
TTL has passed since the conversation's last turn."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
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
from orbit.agent.domain.sessions import SavedCopy, SessionRecord
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


async def saved_copies(store: SessionStore | None, open_id: str) -> list[Recorded]:
    """The copies recorded in the sessions saved in ``store`` but the open one, ``open_id``;
    none without a store, or when the sessions can't be listed, as nothing is known of them."""
    if store is None:
        return []
    try:
        stored = await store.sessions()
    except ReadFailed:
        return []
    return [
        saved.record.copies
        for saved in stored
        if saved.id != open_id and isinstance(saved.record, SessionRecord)
    ]


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
        was removed meanwhile, else a new one; raises :class:`TreeFailed` as making one does."""
        saved = self._held.get(role)
        path = saved.path if saved and await self._exists(saved.path) else await self._make()
        self._held[role] = SavedCopy(path=path, last_turn=self._clock())
        return path

    async def drop(self, role: str) -> None:
        """Remove role ``role``'s copy, if it has one, with its conversation."""
        saved = self._held.pop(role, None)
        if saved is not None:
            await self._remove(saved.path)

    async def expire(self, ttls: Mapping[str, float]) -> None:
        """Remove the copies whose role's TTL in ``ttls``, in hours, has passed since their
        conversation's last turn, saving the session if any were."""
        roles = expired_roles(self._held, ttls, self._clock())
        for role in roles:
            await self._remove(self._held.pop(role).path)
        if roles:
            await self.changed()

    async def sweep(self, others: Sequence[Recorded], ttls: Mapping[str, float]) -> None:
        """Remove the copies past their TTL in ``ttls``: this session's, and those recorded in
        the ``others`` sessions, which keep their records until they next take a turn; then
        those left over that no session records."""
        await self.expire(ttls)
        now = self._clock()
        for recorded in others:
            for role in expired_roles(recorded, ttls, now):
                await self._remove(recorded[role].path)
        for path in unrecorded(await self._leftovers(), [self._held, *others]):
            await self._remove(path)

    async def keep(
        self,
        others: Callable[[], Awaitable[Sequence[Recorded]]],
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
