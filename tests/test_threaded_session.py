import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest

from orbit.protocol import (
    PLANNER,
    CommandEnvelope,
    EventEnvelope,
    Notice,
    SendMessage,
    Session,
    ThreadedSession,
)


def command(text: str) -> CommandEnvelope:
    """A message to the planner saying ``text``, in an envelope."""
    return CommandEnvelope(command_id=text, payload=SendMessage(agent=PLANNER, text=text))


def notice(seq: int, message: str) -> EventEnvelope:
    """A notice saying ``message``, in an envelope at ``seq``."""
    return EventEnvelope(seq=seq, payload=Notice(message=message))


class FakeSession:
    """An inner session that records the thread each call ran on."""

    def __init__(
        self,
        events: list[EventEnvelope] | None = None,
        *,
        crash: bool = False,
        endless: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.thread = threading.current_thread()
        self.sent: list[CommandEnvelope] = []
        self.closes = 0
        self.send_delay = 0.0
        self.send_error: Exception | None = None
        self.hang_on_close = False
        self.hold: threading.Event | None = None  # Blocks the backend's loop mid-stream.
        self.stream_finished = threading.Event()
        self.stream_closed = threading.Event()
        self.stream_closed_on = ""
        self._events = events or []
        self._crash = crash
        self._endless = endless

    def _record(self, call: str) -> None:
        self.calls.append((call, threading.current_thread().name))

    async def send(self, envelope: CommandEnvelope) -> None:
        self._record("send")
        time.sleep(self.send_delay)  # Blocks the backend's loop, as heavy backend work would.
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(envelope)

    async def events(self) -> AsyncIterator[EventEnvelope]:
        self._record("events")
        try:
            for event in self._events:
                yield event
            if self.hold is not None:
                self.hold.wait()
                yield notice(len(self._events) + 1, "after the hold")
            if self._crash:
                raise RuntimeError("the queue broke")
            if self._endless:
                await asyncio.Event().wait()
            self.stream_finished.set()
        finally:
            self.stream_closed_on = threading.current_thread().name
            self.stream_closed.set()

    async def close(self) -> None:
        self._record("close")
        self.closes += 1
        if self.hang_on_close:
            await asyncio.Event().wait()


class EchoSession:
    """An inner session with one event stream, however many times ``events`` is called, that
    echoes each command sent to it as a notice."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self._seq = 0

    async def send(self, envelope: CommandEnvelope) -> None:
        self._seq += 1
        await self._queue.put(notice(self._seq, envelope.command_id))

    async def events(self) -> AsyncIterator[EventEnvelope]:
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        pass


def threaded(inner: FakeSession | None = None) -> tuple[ThreadedSession, list[FakeSession]]:
    """A threaded session over ``inner`` (a fresh one by default), and the sessions it built,
    each recording that it was built."""
    built: list[FakeSession] = []

    def make() -> Session:
        session = inner or FakeSession()
        session.calls.append(("build", threading.current_thread().name))
        session.thread = threading.current_thread()
        built.append(session)
        return session

    return ThreadedSession(make), built


async def collect(session: Session) -> list[EventEnvelope]:
    return [event async for event in session.events()]


async def test_commands_and_events_cross_threads_in_order() -> None:
    notices = [notice(n, str(n)) for n in range(1, 21)]
    session, built = threaded(FakeSession(notices))
    commands = [command(str(n)) for n in range(20)]
    for envelope in commands:
        await session.send(envelope)

    assert await collect(session) == notices
    await session.close()
    [inner] = built
    assert inner.sent == commands


async def test_the_inner_session_is_built_and_driven_on_the_backend_thread() -> None:
    session, built = threaded()
    await session.send(command("hi"))
    await collect(session)
    await session.close()

    [inner] = built
    assert threading.current_thread().name != "orbit-backend"
    assert [call for call, _ in inner.calls] == ["build", "send", "events", "close"]
    assert {thread for _, thread in inner.calls} == {"orbit-backend"}


async def test_an_error_from_the_inner_send_re_raises_in_the_caller() -> None:
    inner = FakeSession()
    inner.send_error = ValueError("bad command")
    session, _ = threaded(inner)

    with pytest.raises(ValueError, match="bad command"):
        await session.send(command("hi"))
    await session.close()


async def test_a_crashing_inner_event_stream_raises_after_the_events_before_it() -> None:
    session, _ = threaded(FakeSession([notice(1, "first")], crash=True))
    events = session.events()

    assert await anext(events) == notice(1, "first")
    with pytest.raises(RuntimeError, match="the queue broke"):
        await anext(events)
    await session.close()


async def test_close_closes_the_inner_session_and_joins_the_thread() -> None:
    session, built = threaded()
    session.start()
    [inner] = built
    await session.close()

    assert inner.closes == 1
    assert not inner.thread.is_alive()


async def test_close_twice_closes_once() -> None:
    session, built = threaded()
    session.start()
    await session.close()
    await session.close()

    [inner] = built
    assert inner.closes == 1


async def test_close_before_start_builds_nothing() -> None:
    session, built = threaded()
    await session.close()

    assert built == []
    with pytest.raises(RuntimeError, match="closed"):
        session.start()


async def test_close_ends_an_endless_event_stream() -> None:
    session, _ = threaded(FakeSession([notice(1, "one")], endless=True))
    events = session.events()
    assert await anext(events) == notice(1, "one")
    await session.close()

    with pytest.raises(StopAsyncIteration):
        await anext(events)


async def test_a_session_that_fails_to_build_raises_and_still_closes() -> None:
    def make() -> Session:
        raise OSError("no backend")

    session = ThreadedSession(make)
    with pytest.raises(RuntimeError, match="couldn't be built") as raised:
        session.start()
    assert isinstance(raised.value.__cause__, OSError)

    await session.close()
    await session.close()


async def test_a_cancelled_close_still_stops_the_thread() -> None:
    inner = FakeSession()
    inner.hang_on_close = True
    session, _ = threaded(inner)
    session.start()
    closing = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not inner.thread.is_alive()


async def test_the_thread_context_wraps_the_backend_thread() -> None:
    entered: list[str] = []
    exited: list[str] = []

    @contextmanager
    def context() -> Iterator[None]:
        entered.append(threading.current_thread().name)
        yield
        exited.append(threading.current_thread().name)

    session = ThreadedSession(FakeSession, context)
    session.start()
    assert (entered, exited) == (["orbit-backend"], [])
    await session.close()

    assert exited == ["orbit-backend"]


async def test_a_blocked_backend_does_not_stop_the_callers_loop() -> None:
    inner = FakeSession()
    inner.send_delay = 0.3
    session, _ = threaded(inner)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    await session.send(command("slow"))
    ticker.cancel()
    await session.close()

    assert ticks >= 10


async def test_abandoning_events_stops_its_pump_before_close() -> None:
    inner = FakeSession([notice(1, "one")], endless=True)
    session, _ = threaded(inner)
    events = session.events()
    assert await anext(events) == notice(1, "one")
    await events.aclose()

    assert await asyncio.to_thread(inner.stream_closed.wait, 5)
    assert inner.stream_closed_on == "orbit-backend"
    await session.close()


async def test_events_after_an_abandoned_iterator_receive_every_event() -> None:
    session = ThreadedSession(EchoSession)
    first = session.events()
    await session.send(command("first"))
    assert await anext(first) == notice(1, "first")
    await first.aclose()

    second = session.events()
    for n in range(10):
        await session.send(command(str(n)))
    received = [await asyncio.wait_for(anext(second), 5) for _ in range(10)]
    await session.close()

    assert received == [notice(n + 2, str(n)) for n in range(10)]


async def test_a_pump_whose_callers_loop_has_closed_ends_quietly() -> None:
    inner = FakeSession([notice(1, "one")])
    inner.hold = threading.Event()
    session, _ = threaded(inner)

    async def take_one() -> EventEnvelope:
        return await anext(session.events())

    # The backend's loop is held after "one" until the caller's loop has closed.
    assert await asyncio.to_thread(asyncio.run, take_one()) == notice(1, "one")
    inner.hold.set()
    await session.close()

    assert inner.stream_finished.is_set()
