"""The uvicorn server orbit runs its web servers on, inside its own event loop."""

from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signals to the program it runs in, which decides how it
    stops; uvicorn's own would take ctrl+c for itself alone."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield
