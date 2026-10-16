"""A session that runs another on its own thread and event loop."""

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass
from typing import Any

from orbit.protocol.messages import Command, ErrorOccurred, Event
from orbit.protocol.session import Session

_Deliver = Callable[[Event | None], None]
"""Hands an event, or ``None`` for the end of the stream, to the caller's loop."""


@dataclass(frozen=True)
class _Backend:
    """The inner session and the loop it runs on."""

    loop: asyncio.AbstractEventLoop
    session: Session


class ThreadedSession:
    """A ``Session`` that builds the one ``make_session`` returns, and drives it, on a thread
    of its own named "orbit-backend" with its own event loop, run inside ``thread_context``
    if given, so neither side's work stalls the other's loop.

    The thread starts on ``start`` or the first use; ``close`` ends it.
    """

    def __init__(
        self,
        make_session: Callable[[], Session],
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

    async def send(self, command: Command) -> None:
        backend = self._running()
        await self._on_backend(backend, backend.session.send(command))

    async def events(self) -> AsyncGenerator[Event]:
        backend = self._running()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        pump: list[asyncio.Task[None]] = []  # Touched only on the backend's loop.

        def deliver(event: Event | None) -> None:
            with suppress(RuntimeError):  # The caller's loop has closed: nobody is listening.
                loop.call_soon_threadsafe(queue.put_nowait, event)

        backend.loop.call_soon_threadsafe(self._start_pump, backend.session, deliver, pump)
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            _cancel_on(backend.loop, pump)

    async def close(self) -> None:
        """Close the inner session on its loop, then stop the backend thread and wait for it."""
        if self._closed:
            return
        self._closed = True
        if self._thread is None:
            return
        try:
            if self._backend is not None:
                await self._on_backend(self._backend, self._backend.session.close())
        finally:
            self._stop(self._thread)

    def _running(self) -> _Backend:
        """The running backend, started first if it hasn't been."""
        if self._closed:
            raise RuntimeError("The session is closed.")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="orbit-backend", daemon=True)
            self._thread.start()
            self._ready.wait()
        if self._backend is None:
            raise RuntimeError("The backend session couldn't be built.") from self._error
        return self._backend

    @staticmethod
    async def _on_backend(backend: _Backend, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` on the backend's loop and wait for it."""
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, backend.loop))

    def _stop(self, thread: threading.Thread) -> None:
        """Have the backend's loop stop, and wait for ``thread`` to end."""
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
        """Build the inner session and keep it until the session is closed."""
        with self._thread_context():
            self._backend = _Backend(asyncio.get_running_loop(), self._make_session())
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
    stream fails, an error saying so comes first."""
    try:
        async for event in session.events():
            deliver(event)
    except Exception as exc:
        deliver(ErrorOccurred(message=f"The backend's event stream failed: {exc}"))
    finally:
        deliver(None)
