"""The modal offering a freed repository: take it over or stay read-only."""

from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.confirm import TAKE_OVER_MESSAGE, TAKE_OVER_NO, TAKE_OVER_TITLE, TAKE_OVER_YES
from orbit.tui.view.outcomes import Send
from orbit.tui.view.takeover import Answered, TakeoverOffer
from orbit.tui.widgets import ConfirmScreen


class TakeoverHost:
    """Shows the :class:`TakeoverOffer` it holds as ``offer``, which sends the user's choice
    with ``send``: its modal, one at a time, with ``push``."""

    def __init__(self, push: Push[ConfirmScreen, bool], send: Send) -> None:
        self._slot = ModalSlot[ConfirmScreen, bool](push)
        self.offer = TakeoverOffer(self, send)

    @property
    def modal_open(self) -> bool:
        return self._slot.showing is not None

    def open_modal(self, answered: Answered) -> None:
        screen = ConfirmScreen(TAKE_OVER_TITLE, TAKE_OVER_MESSAGE, TAKE_OVER_YES, TAKE_OVER_NO)
        self._slot.open(screen, answered)

    def close_modal(self) -> None:
        self._slot.close(None)
