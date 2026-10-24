"""How a session halted because storage failed is shown."""

from typing import Protocol

from orbit.protocol import ResumeStorage
from orbit.tui.view.outcomes import Awaited, Send

HALT_TITLE = "Session stopped: storage failed"
HALT_HINT = "enter try again · esc close"

# The writes a halt's explanation names, as the status line names them.
_WRITES = ("event log", "session record", "review count")


def halt_status(explanation: str) -> str:
    """The status line shown by the prompt while halted, naming the write that failed as the
    first line of ``explanation`` names it, or storage when it names none."""
    first = explanation.partition("\n")[0]
    failed = next((write for write in _WRITES if write in first), "storage")
    return f"Session stopped: {failed} failed; see the transcript"


class HaltView(Protocol):
    """Where a halt shows: the status line by the prompt, and the halt's modal, one at a time,
    which the user may close."""

    @property
    def modal_open(self) -> bool:
        """Whether the halt's modal is open."""
        ...

    def show_status(self, status: str | None) -> None:
        """Show ``status`` by the prompt, or hide that line for none."""
        ...

    def open_modal(self, explanation: str) -> None:
        """Open the halt's modal, showing ``explanation``."""
        ...

    def show_in_modal(self, explanation: str) -> None:
        """Show ``explanation`` in the halt's modal, which is open, in place of what it shows."""
        ...

    def close_modal(self) -> None:
        """Close the halt's modal, if it's open."""
        ...


class StorageHalt:
    """Follows whether the session is halted because storage failed, showing it in ``view``
    while it is: the status line, and the halt's modal, one at a time. Trying again sends
    ``ResumeStorage`` with ``send``, and the latest try accepted lifts the halt."""

    def __init__(self, view: HaltView, send: Send) -> None:
        self._view = view
        self._send = send
        self.explanation: str | None = None  # Why the session is halted; none while it isn't.

    def halted(self, explanation: str) -> None:
        """Show that the session is halted for ``explanation``, opening the modal or showing it
        there if it's open."""
        self.explanation = explanation
        self._view.show_status(halt_status(explanation))
        if self._view.modal_open:
            self._view.show_in_modal(explanation)
        else:
            self._view.open_modal(explanation)

    def lifted(self) -> None:
        """Show that the session isn't halted, closing the modal if it's open."""
        self.explanation = None
        self._view.show_status(None)
        self._view.close_modal()

    async def retry(self) -> None:
        """Try again, lifting the halt once the try is accepted, unless a later one is sent."""
        await self._send(ResumeStorage(), Awaited(accepted=self.lifted, supersedes=True))
