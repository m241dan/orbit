"""A session that runs another on its own thread and event loop."""

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass
from typing import Any

from orbit.protocol import CommandEnvelope, EventEnvelope, Session, SharedSession

_Item = EventEnvelope | Exception | None
"""What a pump hands over: an event, the failure that ended the stream, or ``None`` for its
end."""

_Deliver = Callable[[_Item], None]
"""Hands an item of the stream to the caller's loop."""


@dataclass(frozen=True)
class _Backend:
    """The inner session, the connection to it driven here and the loop they run on."""

    loop: asyncio.AbstractEventLoop
    shared: SharedSession
    session: Session


class ThreadedSession:
    """A ``Session`` that builds the one ``make_session`` returns, and drives one connection
    to it, on a thread of its own named "orbit-backend" with its own event loop, run inside
    ``thread_context`` if given, so neither side's work stalls the other's loop.

    The thread starts on ``start`` or the first use; ``close`` closes the inner session and
    ends it.
    """

    def __init__(
        self,
        make_session: Callable[[], SharedSession],
        thread_context: Callable[[], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._make_session = make_session
        self._thread_context = thread_context or nullcontext
        self._thread: threading.Thread | None = None
        self._backend: _Backend | None = None
        self._error: Exception | None = None
        self._ready = threading.Event()
        self._stopping = asyncio.Event()  # Set on, and awaited by, the backend's loop.
        self._pumps: set[asyncio.Task[None]] = set()  # Touched only on the backend's loop.
        self._closed = False

    def start(self) -> None:
        """Start the backend thread, if it isn't running, and wait for the inner session."""
        self._running()

    async def send(self, envelope: CommandEnvelope) -> None:
        backend = await self._started()
        await self._on_backend(backend, backend.session.send(envelope))

    async def events(self) -> AsyncGenerator[EventEnvelope]:
        """The inner session's events; if its stream fails, that failure is raised after the
        events before it."""
        backend = await self._started()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[_Item] = asyncio.Queue()

        pump: list[asyncio.Task[None]] = []  # Touched only on the backend's loop.

        def deliver(item: _Item) -> None:
            with suppress(RuntimeError):  # The caller's loop has closed: nobody is listening.
                loop.call_soon_threadsafe(queue.put_nowait, item)

        backend.loop.call_soon_threadsafe(self._start_pump, backend.session, deliver, pump)
        try:
            while (item := await queue.get()) is not None:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            _cancel_on(backend.loop, pump)

    async def close(self) -> None:
        """Close the inner session on its loop, once it's built, then stop the backend thread
        and wait for it, each waited for on another thread so the caller's loop keeps
        running."""
        if self._closed:
            return
        self._closed = True
        if self._thread is None:
            return
        try:
            await asyncio.to_thread(self._ready.wait)
            if self._backend is not None:
                await self._on_backend(self._backend, self._backend.shared.close())
        finally:
            await asyncio.to_thread(self._stop, self._thread)

    def _running(self) -> _Backend:
        """The running backend, started first if it hasn't been."""
        self._launch()
        self._ready.wait()
        return self._built()

    async def _started(self) -> _Backend:
        """The running backend, started first if it hasn't been, waited for on another thread
        so the caller's loop keeps running while it's built."""
        self._launch()
        if not self._ready.is_set():
            await asyncio.to_thread(self._ready.wait)
        return self._built()

    def _launch(self) -> None:
        """Start the backend thread, if it hasn't been started."""
        if self._closed:
            raise RuntimeError("The session is closed.")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="orbit-backend", daemon=True)
            self._thread.start()

    def _built(self) -> _Backend:
        """The backend, once its thread is ready; it failed to build if there isn't one."""
        if self._backend is None:
            raise RuntimeError("The backend session couldn't be built.") from self._error
        return self._backend

    @staticmethod
    async def _on_backend(backend: _Backend, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` on the backend's loop and wait for it."""
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, backend.loop))

    def _stop(self, thread: threading.Thread) -> None:
        """Have the backend's loop stop, once it's built, and wait for ``thread`` to end."""
        self._ready.wait()
        if self._backend is not None:
            self._backend.loop.call_soon_threadsafe(self._stopping.set)
        thread.join()

    def _run(self) -> None:
        """The backend thread: run its loop until the session is closed."""
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._error = exc
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        """Build the inner session, connect to it and keep it until the session is closed."""
        with self._thread_context():
            shared = self._make_session()
            self._backend = _Backend(asyncio.get_running_loop(), shared, shared.connect())
            self._ready.set()
            await self._stopping.wait()
            for pump in self._pumps:
                pump.cancel()
            await asyncio.gather(*self._pumps, return_exceptions=True)

    def _start_pump(
        self, session: Session, deliver: _Deliver, pump: list[asyncio.Task[None]]
    ) -> None:
        """Start passing ``session``'s events to ``deliver``, adding the task doing so to
        ``pump``."""
        task = asyncio.create_task(_pump(session, deliver))
        task.add_done_callback(self._pumps.discard)
        self._pumps.add(task)
        pump.append(task)


def _cancel_on(loop: asyncio.AbstractEventLoop, tasks: list[asyncio.Task[None]]) -> None:
    """Have ``loop`` cancel ``tasks``, once it has run what was scheduled on it before; if
    ``loop`` has closed, its tasks have already ended."""
    with suppress(RuntimeError):
        loop.call_soon_threadsafe(_cancel_all, tasks)


def _cancel_all(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel each of ``tasks``."""
    for task in tasks:
        task.cancel()


async def _pump(session: Session, deliver: _Deliver) -> None:
    """Pass each of ``session``'s events to ``deliver``, then the end of the stream; if the
    stream fails, its failure comes first."""
    try:
        async for event in session.events():
            deliver(event)
    except Exception as exc:
        deliver(exc)
    finally:
        deliver(None)
