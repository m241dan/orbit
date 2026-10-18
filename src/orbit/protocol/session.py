"""The interface a frontend uses to talk to a backend."""

from collections.abc import AsyncIterator
from typing import Protocol

from orbit.protocol.envelope import CommandEnvelope, EventEnvelope


class Session(Protocol):
    """A live connection to an agent.

    The frontend sends commands and consumes the event stream, each in its envelope; it
    never sees how the backend fulfils them.
    """

    async def send(self, envelope: CommandEnvelope) -> None: ...

    def events(self) -> AsyncIterator[EventEnvelope]: ...

    async def close(self) -> None:
        """Stop any work in progress and release what the backend holds; the session is
        done afterwards."""
        ...
