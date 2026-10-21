"""The slot holding the one modal a host shows at a time."""

from collections.abc import Awaitable, Callable
from typing import Any

from textual.screen import ModalScreen

type Closed[R] = Callable[[R | None], Awaitable[None]]
type Push[S, R] = Callable[[S, Closed[R]], object]


class ModalSlot[S: ModalScreen[Any], R]:
    """Holds the modal shown, pushed with ``push``, until it closes: by itself, passing its
    result on, or from here, passing nothing on."""

    def __init__(self, push: Push[S, R]) -> None:
        self._push = push
        self.showing: S | None = None  # The modal shown, if any.

    def open(self, screen: S, on_result: Closed[R]) -> None:
        """Show ``screen``, calling ``on_result`` with its result once it closes by itself."""
        self.showing = screen

        async def closed(result: R | None) -> None:
            if self.showing is not screen:
                return  # Closed from here already.
            self.showing = None
            await on_result(result)

        self._push(screen, closed)

    def close(self, result: R | None) -> None:
        """Dismiss the modal shown, if any, with ``result``, no longer showing it."""
        screen = self.showing
        if screen is None:
            return
        self.showing = None  # Its callback runs later, after any modal opened since.
        screen.dismiss(result)
