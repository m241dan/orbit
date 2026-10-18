"""The handshake every stream opens with: ``Hello`` answered by ``Welcome``, then what the
frontend needs to show the session."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from orbit import __version__
from orbit.agent.application.outbox import EventSink
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    INCOMPATIBLE,
    INVALID,
    PROTOCOL_VERSION,
    CommandRefused,
    Event,
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
    """What a handshake publishes after ``Welcome``: the ``catalog``, which it publishes, the
    ``latest_usage``, if any was fetched, the ``snapshot`` of the session, the one-off notices
    (the ``workspace``, if any, whether there are ``no_steps`` and the ``leftovers`` of earlier
    runs) and the saved sessions to pick from, which ``list_sessions`` publishes, if any. It
    also ``load_review_count``\\s first, and then has the background ``start``, unless the
    session is ``closing``."""

    catalog: Callable[[], Awaitable[None]]
    latest_usage: Callable[[], UsageChanged | None]
    snapshot: Callable[[], Snapshot]
    load_review_count: Callable[[], Awaitable[None]]
    workspace: str | None
    no_steps: Callable[[], bool]
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
        the latest usage, if any, a snapshot, the one-off notices and, if asked to, the saved
        sessions to pick from; refused for a second Hello, or another protocol version."""
        if self._welcomed:
            raise Refused(INVALID, HELLO_AGAIN_MESSAGE)
        if hello.protocol_version != PROTOCOL_VERSION:
            self._incompatible = True
            message = (
                f"Protocol version {hello.protocol_version} isn't supported; "
                f"this orbit speaks version {PROTOCOL_VERSION}."
            )
            raise Refused(INCOMPATIBLE, message)
        self._welcomed = True
        await self._welcome(command_id)
        try:
            await self._announce()
        finally:  # A sender cancelled meanwhile mustn't keep the background from starting.
            if not self._parts.closing():
                self._parts.start()

    async def _welcome(self, command_id: str) -> None:
        """Answer Hello ``command_id`` with ``Welcome``, then the catalog, the latest usage, if
        any was fetched, and a snapshot of the session."""
        parts = self._parts
        await self._events.put(Welcome(command_id=command_id, server_version=__version__))
        await parts.catalog()
        latest = parts.latest_usage()
        if latest is not None:
            await self._events.put(latest)
        await self._events.put(parts.snapshot())

    async def _announce(self) -> None:
        """Load the review count, then publish the one-off notices and the saved sessions to
        pick from, if asked to."""
        parts = self._parts
        await parts.load_review_count()
        for note in self._notes():
            await self._events.put(Notice(message=note))
        for note in await parts.leftovers():
            await self._events.put(Notice(message=note))
        if parts.list_sessions is not None:
            await parts.list_sessions()

    def _notes(self) -> list[str]:
        """The workspace, if any, and that no steps verify tasks, if none do."""
        workspace = self._parts.workspace
        notes = [f"Workspace: {workspace}"] if workspace is not None else []
        if self._parts.no_steps():
            notes.append(NO_STEPS_NOTE)
        return notes
