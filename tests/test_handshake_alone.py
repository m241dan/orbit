"""Tests for the handshake on its own, with parts that record what it asks of them."""

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import replace

import pytest

from orbit import __version__
from orbit.agent.application.handshake import (
    HELLO_AGAIN_MESSAGE,
    HELLO_FIRST_MESSAGE,
    NO_STEPS_NOTE,
    Handshake,
    HandshakeParts,
    HostGreeting,
)
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    INCOMPATIBLE,
    INTERNAL,
    INVALID,
    PROTOCOL_VERSION,
    UNKNOWN,
    Catalog,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Hello,
    Notice,
    UsageChanged,
    Welcome,
)

CATALOG = Catalog(workspaces=(), agents=(), triggers=(), lanes=())
USAGE = UsageChanged(detail="fetched")
OPEN = "open"
"""The id of the host's open session."""


class Parts:
    """The parts of a control plane and a host as a handshake sees them, logging into ``log``
    the events published, the envelopes resent and, as strings, what else it was asked to do;
    ``missing`` is what the frontend missed, when it can catch up on it."""

    def __init__(self) -> None:
        self.log: list[Event | EventEnvelope | str] = []
        self.missing: Sequence[EventEnvelope] | None = None
        self.latest: UsageChanged | None = None
        self.no_steps = False
        self.notes: list[str] = []
        self.closing = False
        self.last_seq = 20
        self.cancel_at: str | None = None  # The part that's cancelled when it's asked.
        self.fail_at: str | None = None  # The part that fails when it's asked.
        self.refuse_view = False  # Whether viewing a session is refused as unknown.

    async def put(self, item: Event) -> None:
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

    async def leftovers(self) -> Sequence[str]:
        await self._part("leftovers")
        return self.notes

    async def view(self, session: str) -> None:
        if self.refuse_view:
            raise Refused(UNKNOWN, f"No session {session}.")
        await self._part(f"view {session}")

    async def missed(self, since: int) -> Sequence[EventEnvelope] | None:
        await self._part(f"missed since {since}")
        return self.missing

    def resend(self, envelope: EventEnvelope) -> None:
        self.log.append(envelope)

    def start(self) -> None:
        self.log.append("start")

    def handshake(self) -> Handshake:
        parts = HandshakeParts(self.catalog, lambda: self.latest, lambda: self.last_seq)
        return Handshake(parts, self.put, self.resend)

    def greeting(self, workspace: str | None = None) -> HostGreeting:
        return HostGreeting(
            lambda: OPEN,
            self.missed,
            workspace,
            lambda: self.no_steps,
            (),
            self.leftovers,
            self.start,
            lambda: self.closing,
        )

    async def joined(
        self, since: int | None = None, session: str = OPEN, workspace: str | None = None
    ) -> Handshake:
        """A handshake that welcomed a Hello giving ``since`` and joined ``session``, with the
        log holding what the join did alone."""
        handshake = self.handshake()
        await handshake.hello(hello(since=since), "hi")
        self.log.clear()
        await handshake.join(self.greeting(workspace), session, self.view)
        return handshake


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


@pytest.mark.parametrize("version", [PROTOCOL_VERSION - 1, PROTOCOL_VERSION + 1])
async def test_another_version_is_refused_incompatible_and_ends_the_stream(version: int) -> None:
    parts = Parts()
    handshake = parts.handshake()
    with pytest.raises(Refused) as refused:
        await handshake.hello(hello(version), "hi")
    assert refused.value.code == INCOMPATIBLE
    assert str(refused.value) == (
        f"Protocol version {version} isn't supported; this orbit speaks version {PROTOCOL_VERSION}."
    )
    assert handshake.ended and not handshake.welcomed
    assert parts.log == []


async def test_hello_publishes_only_welcome_and_the_catalog_and_attaches_nothing() -> None:
    parts = Parts()
    parts.latest, parts.no_steps, parts.notes = USAGE, True, ["left over"]
    handshake = parts.handshake()
    await handshake.hello(hello(since=15), "hi")
    assert parts.log == ["catalog", welcome(), CATALOG]
    assert not handshake.listening and not handshake.greeted and not handshake.ended


async def test_joining_publishes_everything_in_order_and_starts_the_background() -> None:
    parts = Parts()
    parts.latest, parts.no_steps, parts.notes = USAGE, True, ["left over"]
    handshake = await parts.joined(workspace="/work")
    assert parts.log == [
        f"view {OPEN}",
        USAGE,
        Notice(message="Workspace: /work"),
        Notice(message=NO_STEPS_NOTE),
        "leftovers",
        Notice(message="left over"),
        "start",
    ]
    assert handshake.greeted and handshake.listening


async def test_a_bare_join_views_the_session_and_publishes_no_notes() -> None:
    parts = Parts()
    await parts.joined()
    assert parts.log == [f"view {OPEN}", "leftovers", "start"]


async def test_joining_a_session_that_isnt_open_views_it_and_never_replays() -> None:
    parts = Parts()
    parts.missing = []
    await parts.joined(since=15, session="earlier")
    assert parts.log == ["view earlier", "leftovers", "start"]


async def test_joining_a_session_there_is_none_of_is_refused_and_changes_nothing() -> None:
    parts = Parts()
    parts.refuse_view = True
    handshake = parts.handshake()
    await handshake.hello(hello(), "hi")
    with pytest.raises(Refused):
        await handshake.join(parts.greeting(), "gone", parts.view)
    assert parts.log == ["catalog", welcome(), CATALOG]
    assert not handshake.listening and not handshake.greeted


async def test_the_background_starts_once_even_when_cancelled_while_joining() -> None:
    parts = Parts()
    parts.cancel_at, parts.notes = "leftovers", ["left over"]
    with pytest.raises(asyncio.CancelledError):
        await parts.joined()
    assert parts.log[1:] == ["leftovers", "start"]


def missed(seq: int) -> EventEnvelope:
    return EventEnvelope(seq=seq, session="open", payload=Notice(message=f"missed {seq}"))


async def test_joining_the_open_session_after_a_hello_that_can_catch_up_replays_it() -> None:
    parts = Parts()
    parts.latest, parts.notes = USAGE, ["left over"]
    parts.missing = [missed(16), missed(17)]
    await parts.joined(since=15, workspace="/work")
    assert parts.log == [
        "missed since 15",
        missed(16),
        missed(17),
        USAGE,
        Notice(message="Workspace: /work"),
        "leftovers",
        Notice(message="left over"),
        "start",
    ]


async def test_a_join_after_leaving_gets_the_snapshot_without_asking() -> None:
    parts = Parts()
    parts.missing = [missed(16)]
    handshake = await parts.joined(since=15)
    handshake.leave()
    parts.log.clear()
    await handshake.join(parts.greeting(), OPEN, parts.view)
    assert parts.log == [f"view {OPEN}", "leftovers", "start"]
    assert handshake.greeted


async def test_a_hello_ahead_of_the_last_seq_stamped_gets_the_snapshot_without_asking() -> None:
    parts = Parts()
    parts.missing = []
    await parts.joined(since=21)
    assert parts.log[0] == f"view {OPEN}"


async def test_a_join_that_cant_catch_up_gets_the_snapshot() -> None:
    parts = Parts()
    await parts.joined(since=15)
    assert parts.log[:2] == ["missed since 15", f"view {OPEN}"]


async def test_a_join_with_nothing_missed_gets_no_snapshot() -> None:
    parts = Parts()
    parts.missing = []
    await parts.joined(since=15)
    assert parts.log[:2] == ["missed since 15", "leftovers"]


async def test_what_was_missed_that_cant_be_had_fails_the_join() -> None:
    parts = Parts()
    parts.fail_at = "missed since 15"
    with pytest.raises(RuntimeError, match="no missed since 15"):
        await parts.joined(since=15)
    assert parts.log == ["missed since 15"]


async def test_a_snapshot_that_fails_is_an_internal_error_and_the_join_goes_on() -> None:
    parts = Parts()
    parts.fail_at = f"view {OPEN}"
    handshake = await parts.joined()
    message = f"Orbit failed to finish greeting the frontend: no view {OPEN}"
    assert parts.log[:2] == [f"view {OPEN}", ErrorOccurred(code=INTERNAL, message=message)]
    assert parts.log[-1] == "start" and handshake.greeted


async def test_a_failure_after_the_view_is_an_internal_error_and_the_background_starts() -> None:
    parts = Parts()
    parts.fail_at = "leftovers"
    await parts.joined()
    message = "Orbit failed to finish greeting the frontend: no leftovers"
    error = ErrorOccurred(code=INTERNAL, message=message)
    assert parts.log[-2:] == [error, "start"]


async def test_a_catalog_that_fails_fails_hello_with_no_welcome() -> None:
    parts = Parts()
    parts.fail_at = "catalog"
    handshake = parts.handshake()
    with pytest.raises(RuntimeError, match="no catalog"):
        await handshake.hello(hello(), "hi")
    assert parts.log == ["catalog"]
    assert not handshake.welcomed and not handshake.listening


async def test_a_closing_host_does_not_start_the_background() -> None:
    parts = Parts()
    parts.closing = True
    await parts.joined()
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


async def joining(parts: Watched, since: int) -> Handshake:
    """A handshake of ``parts`` that welcomed a Hello giving ``since``, joining the open
    session; it's left as it is if joining fails."""
    handshake = parts.handshake_seen = parts.handshake()
    await handshake.hello(hello(since=since), "hi")
    assert not handshake.listening
    with contextlib.suppress(RuntimeError):
        await handshake.join(parts.greeting(), OPEN, parts.view)
    return handshake


async def test_events_reach_the_frontend_from_when_what_it_missed_is_read() -> None:
    parts = Watched()
    handshake = await joining(parts, 15)
    assert parts.listening == [True]
    assert handshake.listening


async def test_events_stop_reaching_a_frontend_whose_missed_events_cant_be_had() -> None:
    parts = Watched()
    parts.fail_at = "missed since 15"
    handshake = await joining(parts, 15)
    assert parts.listening == [True]
    assert not handshake.listening


async def test_live_events_held_while_catching_up_follow_what_was_missed() -> None:
    parts = Watched()
    parts.missing = [missed(16)]
    handshake = parts.handshake_seen = parts.handshake()
    await handshake.hello(hello(since=15), "hi")
    held = missed(18)

    async def missed_then_live(since: int) -> Sequence[EventEnvelope] | None:
        handshake.deliver(held)
        assert handshake.held == 1
        return parts.missing

    greeting = parts.greeting()
    parts.log.clear()
    await handshake.join(replace(greeting, missed=missed_then_live), OPEN, parts.view)
    assert parts.log[:2] == [missed(16), held]
    assert handshake.held == 0
