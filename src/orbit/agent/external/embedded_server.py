"""The uvicorn server orbit runs its web servers on, inside its own event loop."""

import asyncio
import socket
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from orbit.agent.application.ports import HostFailed


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signals to the program it runs in, which decides how it
    stops; uvicorn's own would take ctrl+c for itself alone."""

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
