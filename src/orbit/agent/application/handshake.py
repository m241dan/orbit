"""The handshake every stream opens with: ``Hello`` answered by ``Welcome`` and the catalog, then
the first ``Attach``, answered with what the frontend needs to show the session."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from orbit import __version__
from orbit.agent.application.failures import unexpected
from orbit.agent.domain.refusals import Refused, failed_to
from orbit.protocol import (
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    PROTOCOL_VERSION,
    Catalog,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Hello,
    Notice,
    UsageChanged,
    Welcome,
)

NO_STEPS_NOTE = "No verification steps configured. Press ctrl+e to set them up."
HELLO_FIRST_MESSAGE = "Send Hello first."
HELLO_AGAIN_MESSAGE = "Hello was already received."

View = Callable[[str], Awaitable[None]]
"""Has the frontend view the session it's given and publishes its snapshot to it; refused for a
session there's none of."""


@dataclass(frozen=True)
class HandshakeParts:
    """What a handshake publishes after ``Welcome``: the ``catalog``, which it fetches first,
    and, once the frontend attaches, the ``latest_usage``, if any was fetched; ``last_seq`` is
    the last ``seq`` stamped."""

    catalog: Callable[[], Awaitable[Catalog]]
    latest_usage: Callable[[], UsageChanged | None]
    last_seq: Callable[[], int]


@dataclass(frozen=True)
class HostGreeting:
    """What a workspace's host gives the handshake of a connection attaching to it first: its
    ``open_session``'s id, the events of it a frontend ``missed`` since a ``seq`` and the
    one-off notices: the ``workspace``, if any, whether there are ``no_steps``, the
    ``startup_notes`` and the ``leftovers`` of earlier runs. Once it's greeted, the background
    ``start``\\s, unless the host is ``closing``."""

    open_session: Callable[[], str]
    missed: Callable[[int], Awaitable[Sequence[EventEnvelope] | None]]
    workspace: str | None
    no_steps: Callable[[], bool]
    startup_notes: Sequence[str]
    leftovers: Callable[[], Awaitable[Sequence[str]]]
    start: Callable[[], None]
    closing: Callable[[], bool]


class Handshake:
    """Opens a stream with the frontend from ``parts``, having its greeting, from ``Welcome`` on,
    ``greet`` the frontend alone and the events it missed ``resend`` in their envelopes.

    ``Hello`` is answered with ``Welcome`` and the catalog, and nothing more reaches the frontend
    until its first ``Attach``, which :meth:`join` answers. The live events it :meth:`deliver`\\s
    while the frontend is being caught up are held, and resent once it's caught up, so it sees
    them in order.

    Nothing but ``Hello`` is let through until it's answered with ``Welcome``; a second one is
    refused, and one of another protocol version ends the stream.
    """

    def __init__(
        self,
        parts: HandshakeParts,
        greet: Callable[[Event], Awaitable[None]],
        resend: Callable[[EventEnvelope], None],
    ) -> None:
        self._parts = parts
        self._greet = greet
        self._resend = resend
        self._listening = False
        self._held: list[EventEnvelope] | None = None  # None while live events aren't held.
        self._welcomed = False
        self._since: int | None = None  # The last seq the frontend saw, as its Hello said.
        self._greeted = False
        self._incompatible = False  # A Hello was refused: nothing more is published.

    @property
    def welcomed(self) -> bool:
        """Whether a ``Hello`` was welcomed."""
        return self._welcomed

    @property
    def greeted(self) -> bool:
        """Whether the frontend attached to a session and the steps after that have run."""
        return self._greeted

    def leave(self) -> None:
        """Count the frontend attached to no session again, as the workspace it was attached
        to is no longer served; its next ``Attach`` greets it anew, with a snapshot, as the
        ``seq`` its Hello gave was of that workspace's events."""
        self._greeted = False
        self._since = None

    @property
    def listening(self) -> bool:
        """Whether the session's events are delivered to the frontend: from the moment it's
        attaching, and for the open session from the moment what it missed is read, so none
        falls between those and the live ones; those delivered are held until it's caught up."""
        return self._listening

    @property
    def held(self) -> int:
        """How many live events are held until the frontend is caught up."""
        return len(self._held or ())

    def deliver(self, envelope: EventEnvelope) -> None:
        """Resend ``envelope``, a live event of the session, or hold it while the frontend is
        being caught up."""
        if self._held is None:
            self._resend(envelope)
        else:
            self._held.append(envelope)

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
        noting the ``seq`` it gave to catch it up from once it attaches, unless it's ahead of the
        last stamped; refused for a second Hello, or another protocol version, and failing, with
        no Welcome, if the catalog can't be had."""
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
        self._welcomed = True
        since = hello.since
        # Checked now, as the greeting's own seqs would put it behind.
        self._since = since if since is not None and since <= self._parts.last_seq() else None
        await self._greet(Welcome(command_id=command_id, server_version=__version__))
        await self._greet(catalog)

    async def join(self, greeting: HostGreeting, session: str, view: View) -> None:
        """Have the frontend view the session ``session`` of the host ``greeting`` is of, as
        ``view`` does, and publish what follows its first ``Attach``: for the open session, the
        events it missed since the ``seq`` its Hello gave, if it can catch up on them alone,
        then the live events held meanwhile, or else its snapshot; then the latest usage, if
        any, and the one-off notices. Refused, changing nothing, for a session there's none of
        but the open one. A step that fails after the session is viewed is published as an
        ``internal`` error instead, and the steps after it still run."""
        opened = session == greeting.open_session()
        if not opened:
            await view(session)
        missed = await self._listen(greeting if opened else None)
        try:
            if opened:
                await self._attempt(lambda: self._catch_up(missed, lambda: view(session)))
            for step in self._steps(greeting):
                await self._attempt(step)
        finally:  # A sender cancelled meanwhile mustn't keep the background from starting.
            self._joined(greeting)

    def _joined(self, greeting: HostGreeting) -> None:
        """Resend the live events held, count the frontend greeted and start the background
        work of the host ``greeting`` is of, unless it's closing."""
        self._release()
        self._greeted = True
        if not greeting.closing():
            greeting.start()

    async def _listen(self, opened: HostGreeting | None) -> Sequence[EventEnvelope] | None:
        """Have the session's events reach the frontend from now on, and return those of the
        host's open session, if it views that as ``opened`` says, it missed since the ``seq``
        its Hello gave, as :meth:`_missed` does, holding them meanwhile; they stop reaching it,
        and those held are dropped, if those fail."""
        self._listening = True
        self._held = []
        try:
            return None if opened is None else await self._missed(opened)
        except BaseException:
            self._listening = False
            self._held = None
            raise

    def _release(self) -> None:
        """Resend the live events held, if any, and stop holding them."""
        held, self._held = self._held or [], None
        for envelope in held:
            self._resend(envelope)

    async def _missed(self, greeting: HostGreeting) -> Sequence[EventEnvelope] | None:
        """The events of the host ``greeting`` is of the frontend missed since the ``seq`` its
        Hello gave, if it gave one and can catch up on them alone; ``None`` when it needs a
        snapshot."""
        return None if self._since is None else await greeting.missed(self._since)

    async def _catch_up(
        self, missed: Sequence[EventEnvelope] | None, snapshot: Callable[[], Awaitable[None]]
    ) -> None:
        """Resend the events ``missed`` in their envelopes, then the live events held, or, with
        none to catch up on, those held and then the ``snapshot``, which reflects them or comes
        before those after them."""
        for envelope in missed or ():
            self._resend(envelope)
        self._release()
        if missed is None:
            await snapshot()

    def _steps(self, greeting: HostGreeting) -> list[Callable[[], Awaitable[None]]]:
        """The steps after the session is shown, in order: the latest usage and the one-off
        notices of the host ``greeting`` is of."""
        return [
            self._publish_usage,
            lambda: self._publish_notes(greeting),
            lambda: self._publish_leftovers(greeting),
        ]

    async def _attempt(self, step: Callable[[], Awaitable[None]]) -> None:
        """Run ``step``, publishing its failure as an ``internal`` error."""
        try:
            await step()
        except Exception as exc:  # Attach has its outcome; this mustn't be a second.
            message = unexpected(failed_to("finish greeting the frontend", exc), exc)
            await self._greet(ErrorOccurred(code=INTERNAL, message=message))

    async def _publish_usage(self) -> None:
        """Publish the latest usage, if any was fetched."""
        latest = self._parts.latest_usage()
        if latest is not None:
            await self._greet(latest)

    async def _publish_notes(self, greeting: HostGreeting) -> None:
        """Publish the workspace, if any, that no steps verify tasks, if none do, and the startup
        notes, of the host ``greeting`` is of."""
        for note in _notes(greeting):
            await self._greet(Notice(message=note))

    async def _publish_leftovers(self, greeting: HostGreeting) -> None:
        """Publish the leftovers of earlier runs in the host ``greeting`` is of."""
        for note in await greeting.leftovers():
            await self._greet(Notice(message=note))


def _notes(greeting: HostGreeting) -> list[str]:
    """The workspace, if any, that no steps verify tasks, if none do, and the startup notes, of
    the host ``greeting`` is of."""
    workspace = greeting.workspace
    notes = [f"Workspace: {workspace}"] if workspace is not None else []
    if greeting.no_steps():
        notes.append(NO_STEPS_NOTE)
    return [*notes, *greeting.startup_notes]
