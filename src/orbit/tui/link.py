"""The status line and modals that show how the link to a control plane in another process
stands."""

from collections.abc import Callable

from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.link import After, Closed, LinkMonitor
from orbit.tui.widgets import ChoiceScreen


def _link_screen(title: str, message: str, choices: tuple[str, ...]) -> ChoiceScreen:
    """The modal asking ``title`` with ``message`` about the link, offering ``choices``, the
    first picked on escape, bordered as an error."""
    return ChoiceScreen(title, message, choices, escape_index=0).add_class("error")


ShowStatus = Callable[[str | None], None]


class LinkHost:
    """Shows the :class:`LinkMonitor` it holds as ``monitor``, which ends the app with
    ``quit`` and waits with ``after``: its status line with ``show_status`` and its modals,
    one at a time, with ``push``, bordered as an error."""

    def __init__(
        self,
        push: Push[ChoiceScreen, int],
        quit: Callable[[], None],
        show_status: ShowStatus,
        after: After,
    ) -> None:
        self._show_status = show_status
        self._slot = ModalSlot[ChoiceScreen, int](push)
        self.monitor = LinkMonitor(self, quit, after)

    @property
    def shown(self) -> ChoiceScreen | None:
        """The modal asking whether to keep trying, or saying why the link won't be retried, if
        open."""
        return self._slot.showing

    @property
    def modal_open(self) -> bool:
        return self.shown is not None

    def show_status(self, status: str | None) -> None:
        self._show_status(status)

    def open_modal(
        self, title: str, message: str, choices: tuple[str, ...], closed: Closed
    ) -> None:
        self._slot.open(_link_screen(title, message, choices), closed)

    def close_modal(self, result: int | None) -> None:
        self._slot.close(result)
