"""The uvicorn server orbit runs its web servers on, inside its own event loop."""

import asyncio
import logging
import socket
from collections.abc import Iterator, Sized
from contextlib import contextmanager, suppress

import uvicorn

from orbit.agent.application.ports import HostFailed

logger = logging.getLogger("uvicorn.error")
"""Where uvicorn says what a server shutting down waits for."""

TICK_SECONDS = 0.1
"""How often a running server ticks, refreshing the date it sends, as uvicorn's does."""
SETTLE_SECONDS = 0.1
"""How long, at most, a stopping server gives the connections it asked to close before it waits
on them within its graceful shutdown's time limit, as uvicorn's does."""
POLL_SECONDS = 0.005
"""How often a stopping server checks whether its connections and tasks have finished."""
WAITING_FOR_CONNECTIONS = "Waiting for connections to close. (CTRL+C to force quit)"
WAITING_FOR_TASKS = "Waiting for background tasks to complete. (CTRL+C to force quit)"


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signals to the program it runs in, which decides how it
    stops, and stops as soon as it can.

    uvicorn's own would take ctrl+c for itself alone, notice it was told to stop only on its
    next tick, and wait on its connections and tasks a tenth of a second at a time.
    """

    def __init__(self, config: uvicorn.Config) -> None:
        self._exiting = asyncio.Event()  # Set while the server is told to stop.
        super().__init__(config)

    @property
    def should_exit(self) -> bool:
        """Whether the server is told to stop."""
        return self._exiting.is_set()

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        if value:
            self._exiting.set()
        else:
            self._exiting.clear()

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    async def start_serving(self, sock: socket.socket, failure: str) -> asyncio.Task[None]:
        """The task serving on ``sock``, once the server has started.

        Raises :class:`HostFailed` saying ``failure`` if serving ends before it starts. If
        starting fails or is cancelled, serving is stopped and ``sock`` closed.
        """
        serving = asyncio.create_task(self.serve(sockets=[sock]))
        try:
            await self._started(serving, failure)
        except BaseException:
            self.should_exit = True
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            sock.close()
            raise
        return serving

    async def _started(self, serving: asyncio.Task[None], failure: str) -> None:
        """Wait until the server has started; raises :class:`HostFailed` saying ``failure``,
        from what ended it, if ``serving`` ends first."""
        while not self.started:
            if serving.done():
                raise HostFailed(failure) from serving.exception()
            await asyncio.sleep(0.01)

    async def main_loop(self) -> None:
        """Tick as uvicorn's server does until told to stop, which it notices at once."""
        counter = 0
        should_exit = await self.on_tick(counter)
        while not should_exit:
            counter = (counter + 1) % 864000
            with suppress(TimeoutError):
                async with asyncio.timeout(TICK_SECONDS):
                    await self._exiting.wait()
            should_exit = await self.on_tick(counter)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Shut down as uvicorn's server does: stop accepting connections, ask each one open to
        close, give them a moment, then wait for them and the tasks within the graceful
        shutdown's time limit, cancelling the tasks past it, and shut the app's lifespan down,
        unless forced to exit. It waits only as long as they take."""
        logger.info("Shutting down")
        self._close(sockets)
        with suppress(TimeoutError):
            async with asyncio.timeout(SETTLE_SECONDS):
                await self._drained(self.server_state.connections)
        try:
            limit = self.config.timeout_graceful_shutdown
            await asyncio.wait_for(self._wait_tasks_to_complete(), timeout=limit)
        except TimeoutError:
            self._cancel_tasks()
        if not self.force_exit:
            await self.lifespan.shutdown()

    def _close(self, sockets: list[socket.socket] | None) -> None:
        """Stop accepting connections, closing ``sockets`` too, and ask each one open to
        close."""
        for server in self.servers:
            server.close()
        for sock in sockets or []:
            sock.close()
        for connection in list(self.server_state.connections):
            connection.shutdown()

    def _cancel_tasks(self) -> None:
        """Cancel the tasks still running past the graceful shutdown's time limit."""
        tasks = self.server_state.tasks
        logger.error("Cancel %s running task(s), timeout graceful shutdown exceeded", len(tasks))
        for task in tasks:
            task.cancel(msg="Task cancelled, timeout graceful shutdown exceeded")

    async def _wait_tasks_to_complete(self) -> None:
        """Wait for the connections to close, then the tasks to finish, unless forced to exit,
        then for the listening servers to close."""
        await self._waiting(self.server_state.connections, WAITING_FOR_CONNECTIONS)
        await self._waiting(self.server_state.tasks, WAITING_FOR_TASKS)
        for server in self.servers:
            await server.wait_closed()

    async def _waiting(self, pending: Sized, message: str) -> None:
        """Say ``message`` and wait until ``pending`` is empty, if it isn't, unless forced to
        exit."""
        if pending and not self.force_exit:
            logger.info(message)
            await self._drained(pending)

    async def _drained(self, pending: Sized) -> None:
        """Wait until ``pending`` is empty, unless forced to exit."""
        while pending and not self.force_exit:
            await asyncio.sleep(POLL_SECONDS)
