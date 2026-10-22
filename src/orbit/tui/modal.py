"""The slot holding the one modal a host shows at a time, and closing a modal wherever it is."""

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
        """Close the modal shown, if any, with ``result``, no longer showing it."""
        screen = self.showing
        if screen is None:
            return
        self.showing = None  # Its callback runs later, after any modal opened since.
        close_modal(screen, result)


def close_modal[R](screen: ModalScreen[R], result: R | None) -> None:
    """Close ``screen``: dismiss it with ``result`` if it's on top, calling back with it; take it
    out of the screen stack if another modal is over it, deliberately not calling back, as
    nothing answered it; and leave it if it's closed already."""
    # Textual's dismiss pops the top screen whichever screen it's called on.
    if screen.is_active:
        screen.dismiss(result)
        return
    app = screen.app
    stack = app._screen_stack
    if screen not in stack:
        return
    stack.remove(screen)
    screen._pop_result_callback()
    app.call_later(app._replace_screen, screen)
