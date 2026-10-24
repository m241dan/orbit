"""Handing the repository over: what a read-only orbit does as the user answers the offer to
own it."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orbit.agent.application.outbox import EventSink
from orbit.agent.domain.ownership import OWNED_NOTE
from orbit.protocol import (
    INTERNAL,
    ErrorOccurred,
    Notice,
    OwnershipDeclined,
    OwnershipTaken,
    TakeOwnership,
)


class Offer(Protocol):
    """The offer of the repository, as a ``Takeover`` holds it."""

    def answer(self, take: bool) -> None: ...


class Queue(Protocol):
    """The work queued while read-only, as ``WorkCommands`` holds it."""

    def taking(self) -> None: ...

    def taken(self) -> None: ...

    async def own(self, command_id: str) -> None: ...


@dataclass(frozen=True)
class HandoverParts:
    """What a handover acts on: the ``offer`` it answers, the ``queue`` of work it runs once
    owning, the ``events`` it publishes into, ``owned``, called as the repository is taken,
    ``publish_catalog``, which sends every listening connection the catalog, ``recover``, which
    imports the fallback files the owner left and returns notes on them, ``recount``, which
    reads the repository's super review count again, and ``kick``, which starts the reviews
    waiting."""

    offer: Offer
    queue: Queue
    events: EventSink
    owned: Callable[[], None]
    publish_catalog: Callable[[], Awaitable[None]]
    recover: Callable[[], Awaitable[Sequence[str]]]
    recount: Callable[[], Awaitable[None]]
    kick: Callable[[], None]


class Handover:
    """Answers ``TakeOwnership`` with the parts in ``parts``.

    Declining it publishes ``OwnershipDeclined``. Taking it calls ``owned``, then publishes
    ``OwnershipTaken``, a notice and a ``Catalog`` with this workspace no longer read-only. It
    then runs ``recover``, which imports the fallback files the owner left into the log, as the
    owner's startup does, and publishes a notice for each of the notes it returns; one that
    fails, however it does, is reported as an ``ErrorOccurred`` coded ``internal``, its files
    left for the next owner's start, and the takeover goes on. It then reads the super review
    count the owner left again; announcing failing otherwise is reported the same way, the rest
    of the announcement skipped. It then runs the work queued, in order, and kicks the reviews
    waiting, even if announcing the ownership or running the queued work fails.
    """

    def __init__(self, parts: HandoverParts) -> None:
        self._parts = parts

    async def answer(self, command: TakeOwnership, command_id: str) -> None:
        """Answer the offer of the repository: own it, if ``command`` takes it, and run the work
        queued for ``command_id``; refused when no offer is open."""
        self._parts.offer.answer(command.take)
        if not command.take:
            await self._parts.events.put(OwnershipDeclined())
            return
        # Work sent from here on joins the queue, so it runs after the work queued before.
        self._parts.queue.taking()
        try:
            await self._announce_reporting()
        finally:
            # Owned even if announcing it failed, and no offer comes again to run the queue.
            try:
                await self._run_queued(command_id)
            finally:
                self._parts.queue.taken()

    async def _run_queued(self, command_id: str) -> None:
        """Run the work queued for ``command_id``, then kick the reviews waiting, even if
        running the queued work fails."""
        try:
            await self._parts.queue.own(command_id)
        finally:  # After the queued work, so its tasks get agents first, even if it fails.
            self._parts.kick()

    async def _announce_reporting(self) -> None:
        """Announce the ownership, reporting a failure as an ``ErrorOccurred`` coded
        ``internal``, as nothing waits on its answer and the queued work runs regardless."""
        try:
            await self._announce()
        except Exception as exc:
            await self._report(exc)

    async def _report(self, exc: Exception) -> None:
        """Publish ``exc`` as an ``ErrorOccurred`` coded ``internal``."""
        await self._parts.events.put(ErrorOccurred(code=INTERNAL, message=str(exc)))

    async def _announce(self) -> None:
        """Say this orbit owns the repository now, send the catalog saying so, recover the
        fallback files the owner left and read the super review count it left."""
        self._parts.owned()
        await self._parts.events.put(OwnershipTaken())
        await self._parts.events.put(Notice(message=OWNED_NOTE))
        await self._parts.publish_catalog()  # Other workspaces' connections learn it here.
        await self._recover_parked()
        await self._parts.recount()

    async def _recover_parked(self) -> None:
        """Recover the fallback files the owner left, publishing a notice for each recovered or
        an error if that failed, however it did."""
        try:
            notes = await self._parts.recover()
        except Exception as exc:
            await self._report(exc)
            return
        for note in notes:
            await self._parts.events.put(Notice(message=note))
