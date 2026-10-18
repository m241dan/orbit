"""The handshake every stream opens with: ``Hello`` answered by ``Welcome``, then what the
frontend needs to show the session."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from orbit import __version__
from orbit.agent.application.outbox import EventSink
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    PROTOCOL_VERSION,
    Catalog,
    CommandRefused,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Hello,
    Notice,
    Snapshot,
    UsageChanged,
    Welcome,
)

NO_STEPS_NOTE = "No verification steps configured. Press ctrl+e to set them up."
HELLO_FIRST_MESSAGE = "Send Hello first."
HELLO_AGAIN_MESSAGE = "Hello was already received."


def ends_stream(event: Event) -> bool:
    """Whether ``event`` is the last of the stream: an incompatible ``Hello``'s refusal."""
    return isinstance(event, CommandRefused) and event.code == INCOMPATIBLE


@dataclass(frozen=True)
class HandshakeParts:
    """What a handshake publishes after ``Welcome``: the ``catalog``, which it fetches first, the
    ``latest_usage``, if any was fetched, the ``snapshot`` of the session, or, for a ``Hello``
    whose ``since`` is a ``seq`` the frontend can catch up from, the events it ``missed``, which
    it fetches first too and ``resend``\\s in their envelopes, the one-off notices
    (the ``workspace``, if any, whether there are ``no_steps``, the ``startup_notes`` and the
    ``leftovers`` of earlier runs) and the saved sessions to pick from, which ``list_sessions``
    publishes, if any. It also ``load_review_count``\\s first, and then has the background
    ``start``, unless the session is ``closing``."""

    catalog: Callable[[], Awaitable[Catalog]]
    latest_usage: Callable[[], UsageChanged | None]
    snapshot: Callable[[], Awaitable[Snapshot]]
    missed: Callable[[int], Awaitable[Sequence[EventEnvelope] | None]]
    resend: Callable[[EventEnvelope], None]
    load_review_count: Callable[[], Awaitable[None]]
    workspace: str | None
    no_steps: Callable[[], bool]
    startup_notes: Sequence[str]
    leftovers: Callable[[], Awaitable[Sequence[str]]]
    list_sessions: Callable[[], Awaitable[None]] | None
    start: Callable[[], None]
    closing: Callable[[], bool]


class Handshake:
    """Opens a stream with the frontend from ``parts``, publishing into ``events``.

    Nothing but ``Hello`` is let through until it's answered with ``Welcome``; a second one is
    refused, and one of another protocol version ends the stream.
    """

    def __init__(self, parts: HandshakeParts, events: EventSink) -> None:
        self._parts = parts
        self._events = events
        self._welcomed = False
        self._incompatible = False  # A Hello was refused: nothing more is published.

    @property
    def welcomed(self) -> bool:
        """Whether a ``Hello`` was welcomed."""
        return self._welcomed

    @property
    def ended(self) -> bool:
        """Whether a ``Hello`` of another protocol version ended the stream."""
        return self._incompatible

    def admit(self) -> None:
        """Refuse a command other than ``Hello`` until a ``Hello`` is welcomed."""
        if not self._welcomed:
            raise Refused(INVALID, HELLO_FIRST_MESSAGE)

    async def hello(self, hello: Hello, command_id: str) -> None:
        """Welcome the frontend that sent ``hello`` as ``command_id``, then publish the catalog,
        the latest usage, if any, a snapshot, or the events it missed since the ``seq`` it gave,
        if it can catch up on them alone, the one-off notices and, if asked to, the saved
        sessions to pick from; refused for a second Hello, or another protocol version, and
        failing, with no Welcome, if the catalog can't be had. A step that fails after Welcome
        is published as an ``internal`` error instead, and the steps after it still run."""
        if self._welcomed:
            raise Refused(INVALID, HELLO_AGAIN_MESSAGE)
        if hello.protocol_version != PROTOCOL_VERSION:
            self._incompatible = True
            message = (
                f"Protocol version {hello.protocol_version} isn't supported; "
                f"this orbit speaks version {PROTOCOL_VERSION}."
            )
            raise Refused(INCOMPATIBLE, message)
        catalog = await self._parts.catalog()  # Before Welcome, so its failure refuses Hello.
        missed = await self._missed(hello.since)  # Before Welcome, which comes after them.
        self._welcomed = True
        await self._events.put(Welcome(command_id=command_id, server_version=__version__))
        try:
            for step in self._steps(catalog, missed):
                await self._attempt(step)
        finally:  # A sender cancelled meanwhile mustn't keep the background from starting.
            if not self._parts.closing():
                self._parts.start()

    async def _missed(self, since: int | None) -> Sequence[EventEnvelope] | None:
        """The events the frontend missed since ``since``, if it gave one and can catch up on
        them alone; ``None`` when it needs a snapshot."""
        return None if since is None else await self._parts.missed(since)

    async def _publish(self, catalog: Catalog, missed: Sequence[EventEnvelope] | None) -> None:
        """Publish ``catalog``, the latest usage, if any was fetched, and the events ``missed``
        in their envelopes, or, with none to catch up on, a snapshot of the session."""
        parts = self._parts
        await self._events.put(catalog)
        latest = parts.latest_usage()
        if latest is not None:
            await self._events.put(latest)
        if missed is None:
            await self._events.put(await parts.snapshot())
            return
        for envelope in missed:
            parts.resend(envelope)

    def _steps(
        self, catalog: Catalog, missed: Sequence[EventEnvelope] | None
    ) -> list[Callable[[], Awaitable[None]]]:
        """The steps after Welcome, in order: publishing ``catalog`` and the session, or the
        events ``missed``, loading the review count, the one-off notices and, if asked to, the
        saved sessions to pick from."""
        parts = self._parts
        steps = [
            lambda: self._publish(catalog, missed),
            parts.load_review_count,
            self._publish_notes,
            self._publish_leftovers,
        ]
        if parts.list_sessions is not None:
            steps.append(parts.list_sessions)
        return steps

    async def _attempt(self, step: Callable[[], Awaitable[None]]) -> None:
        """Run ``step``, publishing its failure as an ``internal`` error."""
        try:
            await step()
        except Exception as exc:  # Hello has its outcome, Welcome; this mustn't be a second.
            message = f"Orbit failed to finish greeting the frontend: {exc}"
            await self._events.put(ErrorOccurred(code=INTERNAL, message=message))

    async def _publish_notes(self) -> None:
        """Publish the workspace, if any, that no steps verify tasks, if none do, and the startup
        notes."""
        for note in self._notes():
            await self._events.put(Notice(message=note))

    async def _publish_leftovers(self) -> None:
        """Publish the leftovers of earlier runs."""
        for note in await self._parts.leftovers():
            await self._events.put(Notice(message=note))

    def _notes(self) -> list[str]:
        """The workspace, if any, that no steps verify tasks, if none do, and the startup notes."""
        workspace = self._parts.workspace
        notes = [f"Workspace: {workspace}"] if workspace is not None else []
        if self._parts.no_steps():
            notes.append(NO_STEPS_NOTE)
        return [*notes, *self._parts.startup_notes]
