"""Tests for the handshake on its own, with parts that record what it asks of them."""

import asyncio
from collections.abc import Sequence

import pytest

from orbit import __version__
from orbit.agent.application.handshake import (
    HELLO_AGAIN_MESSAGE,
    HELLO_FIRST_MESSAGE,
    NO_STEPS_NOTE,
    Handshake,
    HandshakeParts,
)
from orbit.agent.domain.refusals import Refused
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
    SessionSummary,
    Settings,
    Snapshot,
    UsageChanged,
    Welcome,
)

CATALOG = Catalog(workspaces=(), agents=(), triggers=(), lanes=())
SNAPSHOT = Snapshot(
    session=SessionSummary(id="open", name="open"),
    settings=Settings(),
    model_choices=(),
    busy={},
    tasks=(),
    steps=(),
    review=None,
    proposals=(),
    agents=(),
)
USAGE = UsageChanged(detail="fetched")
LISTED = Notice(message="listed")


class Parts:
    """The parts of a session as a handshake sees them, logging into ``log`` the events
    published, the envelopes resent and, as strings, what else it was asked to do; ``missing``
    is what the frontend missed, when it can catch up on it."""

    def __init__(self) -> None:
        self.log: list[Event | EventEnvelope | str] = []
        self.missing: Sequence[EventEnvelope] | None = None
        self.latest: UsageChanged | None = None
        self.no_steps = False
        self.notes: list[str] = []
        self.closing = False
        self.cancel_at: str | None = None  # The part that's cancelled when it's asked.
        self.fail_at: str | None = None  # The part that fails when it's asked.

    async def put(self, item: Event) -> None:
        self.log.append(item)

    def put_nowait(self, item: Event) -> None:
        self.log.append(item)

    async def _part(self, name: str) -> None:
        self.log.append(name)
        if name == self.cancel_at:
            raise asyncio.CancelledError
        if name == self.fail_at:
            raise RuntimeError(f"no {name}")

    async def catalog(self) -> Catalog:
        await self._part("catalog")
        return CATALOG

    async def load_review_count(self) -> None:
        await self._part("review count")

    async def leftovers(self) -> Sequence[str]:
        await self._part("leftovers")
        return self.notes

    async def list_sessions(self) -> None:
        await self._part("list")
        self.log.append(LISTED)

    async def snapshot(self) -> Snapshot:
        return SNAPSHOT

    async def missed(self, since: int) -> Sequence[EventEnvelope] | None:
        await self._part(f"missed since {since}")
        return self.missing

    def resend(self, envelope: EventEnvelope) -> None:
        self.log.append(envelope)

    def start(self) -> None:
        self.log.append("start")

    def handshake(self, workspace: str | None = None, picking: bool = False) -> Handshake:
        parts = HandshakeParts(
            self.catalog,
            lambda: self.latest,
            self.snapshot,
            self.missed,
            self.load_review_count,
            workspace,
            lambda: self.no_steps,
            (),
            self.leftovers,
            self.list_sessions if picking else None,
            self.start,
            lambda: self.closing,
        )
        return Handshake(parts, self, self.resend)


def hello(protocol_version: int = PROTOCOL_VERSION, since: int | None = None) -> Hello:
    return Hello(client="orbit-tests", protocol_version=protocol_version, since=since)


def welcome(command_id: str = "hi") -> Welcome:
    return Welcome(command_id=command_id, server_version=__version__)


def test_a_command_before_hello_is_refused_invalid() -> None:
    handshake = Parts().handshake()
    with pytest.raises(Refused) as refused:
        handshake.admit()
    assert (refused.value.code, str(refused.value)) == (INVALID, HELLO_FIRST_MESSAGE)
    assert not handshake.welcomed


async def test_a_command_after_hello_is_admitted() -> None:
    handshake = Parts().handshake()
    await handshake.hello(hello(), "hi")
    handshake.admit()
    assert handshake.welcomed


async def test_a_second_hello_is_refused_invalid_and_publishes_nothing() -> None:
    parts = Parts()
    handshake = parts.handshake()
    await handshake.hello(hello(), "hi")
    before = list(parts.log)
    with pytest.raises(Refused) as refused:
        await handshake.hello(hello(), "again")
    assert (refused.value.code, str(refused.value)) == (INVALID, HELLO_AGAIN_MESSAGE)
    assert parts.log == before


async def test_another_version_is_refused_incompatible_and_ends_the_stream() -> None:
    parts = Parts()
    handshake = parts.handshake()
    with pytest.raises(Refused) as refused:
        await handshake.hello(hello(PROTOCOL_VERSION + 1), "hi")
    assert refused.value.code == INCOMPATIBLE
    assert str(refused.value) == (
        f"Protocol version {PROTOCOL_VERSION + 1} isn't supported; "
        f"this orbit speaks version {PROTOCOL_VERSION}."
    )
    assert handshake.ended and not handshake.welcomed
    assert parts.log == []


async def test_a_good_hello_publishes_everything_in_order_and_starts_the_background() -> None:
    parts = Parts()
    parts.latest, parts.no_steps, parts.notes = USAGE, True, ["left over"]
    handshake = parts.handshake(workspace="/work", picking=True)
    await handshake.hello(hello(), "hi")
    assert parts.log == [
        "catalog",
        welcome(),
        CATALOG,
        USAGE,
        SNAPSHOT,
        "review count",
        Notice(message="Workspace: /work"),
        Notice(message=NO_STEPS_NOTE),
        "leftovers",
        Notice(message="left over"),
        "list",
        LISTED,
        "start",
    ]
    assert not handshake.ended


async def test_a_bare_hello_publishes_only_welcome_the_catalog_and_the_snapshot() -> None:
    parts = Parts()
    await parts.handshake().hello(hello(), "hi")
    assert parts.log == [
        "catalog",
        welcome(),
        CATALOG,
        SNAPSHOT,
        "review count",
        "leftovers",
        "start",
    ]


async def test_the_background_starts_once_even_when_cancelled_after_welcome() -> None:
    parts = Parts()
    parts.cancel_at = "review count"
    handshake = parts.handshake()
    with pytest.raises(asyncio.CancelledError):
        await handshake.hello(hello(), "hi")
    assert parts.log[1] == welcome()
    assert parts.log.count("start") == 1
    assert "leftovers" not in parts.log
    assert handshake.welcomed


def missed(seq: int) -> EventEnvelope:
    return EventEnvelope(seq=seq, session="open", payload=Notice(message=f"missed {seq}"))


async def test_a_hello_that_can_catch_up_gets_what_it_missed_in_place_of_the_snapshot() -> None:
    parts = Parts()
    parts.latest, parts.notes = USAGE, ["left over"]
    parts.missing = [missed(16), missed(17)]
    await parts.handshake(workspace="/work").hello(hello(since=15), "hi")
    assert parts.log == [
        "catalog",
        "missed since 15",
        welcome(),
        CATALOG,
        USAGE,
        missed(16),
        missed(17),
        "review count",
        Notice(message="Workspace: /work"),
        "leftovers",
        Notice(message="left over"),
        "start",
    ]


async def test_a_hello_that_cant_catch_up_gets_the_snapshot() -> None:
    parts = Parts()
    await parts.handshake().hello(hello(since=15), "hi")
    assert parts.log[:5] == ["catalog", "missed since 15", welcome(), CATALOG, SNAPSHOT]


async def test_a_hello_with_nothing_missed_gets_no_snapshot() -> None:
    parts = Parts()
    parts.missing = []
    await parts.handshake().hello(hello(since=15), "hi")
    assert parts.log[:5] == ["catalog", "missed since 15", welcome(), CATALOG, "review count"]


async def test_what_was_missed_that_cant_be_had_fails_hello_with_no_welcome() -> None:
    parts = Parts()
    parts.fail_at = "missed since 15"
    handshake = parts.handshake()
    with pytest.raises(RuntimeError, match="no missed since 15"):
        await handshake.hello(hello(since=15), "hi")
    assert parts.log == ["catalog", "missed since 15"]
    assert not handshake.welcomed


async def test_a_failure_after_welcome_is_an_internal_error_and_the_background_starts() -> None:
    parts = Parts()
    parts.fail_at = "leftovers"
    handshake = parts.handshake(picking=True)
    await handshake.hello(hello(), "hi")
    message = "Orbit failed to finish greeting the frontend: no leftovers"
    error = ErrorOccurred(code=INTERNAL, message=message)
    assert parts.log[-4:] == [error, "list", LISTED, "start"]
    assert handshake.welcomed


async def test_a_review_count_that_fails_still_publishes_the_steps_after_it() -> None:
    parts = Parts()
    parts.fail_at, parts.no_steps, parts.notes = "review count", True, ["left over"]
    await parts.handshake(picking=True).hello(hello(), "hi")
    message = "Orbit failed to finish greeting the frontend: no review count"
    assert parts.log[4:] == [
        "review count",
        ErrorOccurred(code=INTERNAL, message=message),
        Notice(message=NO_STEPS_NOTE),
        "leftovers",
        Notice(message="left over"),
        "list",
        LISTED,
        "start",
    ]


async def test_a_catalog_that_fails_fails_hello_with_no_welcome_and_no_background() -> None:
    parts = Parts()
    parts.fail_at = "catalog"
    handshake = parts.handshake()
    with pytest.raises(RuntimeError, match="no catalog"):
        await handshake.hello(hello(), "hi")
    assert parts.log == ["catalog"]
    assert not handshake.welcomed


async def test_a_closing_session_does_not_start_the_background() -> None:
    parts = Parts()
    parts.closing = True
    await parts.handshake().hello(hello(), "hi")
    assert "start" not in parts.log


class Watched(Parts):
    """Parts that note whether the handshake was ``listening`` as what was missed was read."""

    def __init__(self) -> None:
        super().__init__()
        self.handshake_seen: Handshake | None = None
        self.listening: list[bool] = []

    async def missed(self, since: int) -> Sequence[EventEnvelope] | None:
        assert self.handshake_seen is not None
        self.listening.append(self.handshake_seen.listening)
        return await super().missed(since)


async def test_events_reach_the_frontend_from_when_what_it_missed_is_read() -> None:
    parts = Watched()
    handshake = parts.handshake_seen = parts.handshake()
    assert not handshake.listening
    await handshake.hello(hello(since=15), "hi")
    assert parts.listening == [True]
    assert handshake.listening


async def test_events_stop_reaching_a_frontend_whose_missed_events_cant_be_had() -> None:
    parts = Watched()
    parts.fail_at = "missed since 15"
    handshake = parts.handshake_seen = parts.handshake()
    with pytest.raises(RuntimeError):
        await handshake.hello(hello(since=15), "hi")
    assert parts.listening == [True]
    assert not handshake.listening


async def test_events_never_reach_a_frontend_whose_catalog_fails() -> None:
    parts = Parts()
    parts.fail_at = "catalog"
    handshake = parts.handshake()
    with pytest.raises(RuntimeError):
        await handshake.hello(hello(), "hi")
    assert not handshake.listening
