"""The interface a frontend uses to talk to a backend."""

from collections.abc import AsyncIterator
from typing import Protocol

from orbit.protocol.messages import Command, Event


class Session(Protocol):
    """A live connection to an agent.

    The frontend sends commands and consumes the event stream; it never sees
    how the backend fulfils them.
    """

    async def send(self, command: Command) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...

    async def close(self) -> None:
        """Stop any work in progress and release what the backend holds; the session is
        done afterwards."""
        ...
