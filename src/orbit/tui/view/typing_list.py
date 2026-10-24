"""A picker's list with a text box under it, hidden until the user types in it, without the screen
showing them: which keys show under the box, where a refusal shows and what escape does."""

from collections.abc import Sequence
from typing import ClassVar, Protocol

from rich.text import Text

from orbit.tui.view.common import ListHints, kept_highlight, picker_hint

type Label = Text | str
"""What a row shows."""


class TypingListView[T](Protocol):
    """What a picker shows and what the user has done in it: the widgets, which keep the
    highlight as the user moves it."""

    @property
    def is_attached(self) -> bool:
        """Whether the picker is still open."""
        ...

    @property
    def is_mounted(self) -> bool:
        """Whether the picker is on screen yet, its widgets composed."""
        ...

    @property
    def highlighted(self) -> int | None:
        """The row highlighted, if any."""
        ...

    def show_rows(
        self, keys: Sequence[str | None], labels: Sequence[Label], highlight: int | None
    ) -> None:
        """List ``labels``, the rows with ``keys``, highlighting row ``highlight``, if any."""
        ...

    def show_hint(self, hint: str) -> None:
        """Show the keys ``hint`` names under the picker."""
        ...

    def show_note(self, note: str | None) -> None:
        """Show ``note`` under the list, or hide that line for none."""
        ...

    def show_box(self, shown: bool, text: str) -> None:
        """Show the text box starting from ``text`` and type in it, or hide and empty it and go
        back to the list; either way, the box's own note is cleared."""
        ...

    def show_box_note(self, note: str) -> None:
        """Show ``note`` in the text box's hint."""
        ...

    def dismiss(self, result: T) -> object:
        """Close the picker with ``result``."""
        ...

    def quit(self) -> None:
        """Quit orbit."""
        ...


class TypingList[T]:
    """A picker shown in ``view``: a list, a note under it saying why a change was refused, and a
    text box under that, hidden until the user types in it, with the keys ``HINTS`` names shown
    under the picker, the box's while it's shown, or else the list's, where escape quits at
    ``startup``. Escape leaves the text box, or else closes with ``nothing``, or quits orbit at
    ``startup``, where there's nothing to go back to."""

    HINTS: ClassVar[ListHints]

    def __init__(self, view: TypingListView[T], nothing: T, startup: bool) -> None:
        self._view = view
        self._nothing = nothing
        self.startup = startup
        self._box_hint: str | None = None  # The text box's keys, while it's shown.

    @property
    def hint(self) -> str:
        """The keys shown under the picker."""
        return picker_hint(self.HINTS, startup=self.startup, box=self._box_hint)

    @property
    def typing(self) -> bool:
        """Whether the text box is shown."""
        return self._box_hint is not None

    def back(self) -> None:
        """Leave the text box, or else close with nothing, or quit orbit at startup."""
        if self.typing:
            self._type(None)
        elif self.startup:
            self._view.quit()
        else:
            self._view.dismiss(self._nothing)

    def _type(self, box_hint: str | None, text: str = "") -> None:
        """Show the text box, starting from ``text``, with ``box_hint`` its keys, and type in
        it; or, with no keys, go back to the list. Either way, the box's own note is cleared."""
        self._box_hint = box_hint
        self._view.show_box(box_hint is not None, text)
        self._view.show_hint(self.hint)

    def _relist(
        self, shown: Sequence[str | None], keys: Sequence[str | None], labels: Sequence[Label]
    ) -> None:
        """List ``labels``, the rows with ``keys``, in place of the rows with keys ``shown``,
        the highlight going where ``kept_highlight`` says."""
        highlight = kept_highlight(shown, self._view.highlighted, keys)
        self._view.show_rows(keys, labels, highlight)

    def _refused(self, why: str) -> None:
        """Show ``why`` a change was refused, as ``_show_refusal`` does; nothing once the picker
        is closed."""
        if self._view.is_attached:
            self._show_refusal(why)

    def _show_refusal(self, why: str) -> None:
        """Show ``why`` a change was refused: in the text box's hint while it's shown, or else
        under the list."""
        if self.typing:
            self._view.show_box_note(why)
        else:
            self._view.show_note(why)
