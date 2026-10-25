"""The modals that browse the workspace's reports: their list, and a report in full."""

from collections.abc import Sequence
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from orbit.protocol import ReportShown, ReportSummary
from orbit.tui.modal import ModalSlot, Push
from orbit.tui.view.detail import copied_note
from orbit.tui.view.outcomes import Send
from orbit.tui.view.reports import (
    DELETE_TITLE,
    DELETE_YES,
    NO_REPORTS_ROW,
    REPORT_HINT,
    REPORTS_HINT,
    REPORTS_TITLE,
    ReportShelf,
    delete_message,
    report_markdown,
    report_row,
    report_title,
)
from orbit.tui.widgets import ListScreen, TextScreen, confirm


class ReportScreen(TextScreen):
    """``report`` in full: j/k and the arrow keys scroll, y copies it and escape closes."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("y", "copy", show=False)]

    def __init__(self, report: ReportShown) -> None:
        super().__init__(report_title(report), REPORT_HINT, report_markdown(report))

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.app.notify(copied_note(self.text))


class ReportsScreen(ListScreen[None]):
    """The reports on ``shelf``, newest first: ``j``/``k`` (and the arrows) move, enter shows
    the highlighted report, ``x`` deletes it, asking first, and escape closes."""

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    ReportsScreen { align: center middle; }
    ReportsScreen > Vertical {
        width: 90;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    ReportsScreen OptionList { height: auto; max-height: 30; border: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("x", "delete", show=False),
        Binding("escape", "close", show=False),
    ]

    def __init__(self, shelf: ReportShelf) -> None:
        super().__init__()
        self._shelf = shelf

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = REPORTS_TITLE
            box.border_subtitle = REPORTS_HINT
            yield OptionList(*_rows(self._shelf.reports))

    def show(self, reports: Sequence[ReportSummary]) -> None:
        """List ``reports``, keeping the highlight where it was, as far as it can."""
        options = self.query_one(OptionList)
        highlighted = options.highlighted or 0
        options.clear_options()
        options.add_options(_rows(reports))
        options.highlighted = min(highlighted, len(reports) - 1) if reports else None

    @property
    def chosen(self) -> ReportSummary | None:
        """The report highlighted, if any."""
        highlighted = self.highlighted
        reports = self._shelf.reports
        return None if highlighted is None or highlighted >= len(reports) else reports[highlighted]

    @on(OptionList.OptionSelected)
    async def _show(self, event: OptionList.OptionSelected) -> None:
        # Only a report's row can be selected: the row saying there are none is disabled.
        await self._shelf.show(self._shelf.reports[event.option_index].id)

    def action_delete(self) -> None:
        chosen = self.chosen
        if chosen is None:
            return

        async def delete() -> None:
            await self._shelf.delete(chosen.id)

        confirm(self.app.push_screen, DELETE_TITLE, delete_message(chosen), DELETE_YES, delete)

    def action_close(self) -> None:
        self.dismiss(None)


def _rows(reports: Sequence[ReportSummary]) -> list[Option]:
    """The options listing ``reports``, or saying there are none."""
    if not reports:
        return [Option(NO_REPORTS_ROW, disabled=True)]
    return [Option(report_row(report)) for report in reports]


class ReportsHost:
    """Shows the :class:`ReportShelf` it holds as ``shelf``, which asks for reports with
    ``send``: its list, one at a time, and the reports opened over it, with ``push``."""

    def __init__(self, push: Push[ModalScreen[Any], Any], send: Send) -> None:
        self._push = push
        self._slot = ModalSlot[ReportsScreen, None](push)
        self.shelf = ReportShelf(self, send)

    @property
    def list_open(self) -> bool:
        return self._slot.showing is not None

    def open_list(self) -> None:
        self._slot.open(ReportsScreen(self.shelf), _closed)

    def show_list(self, reports: Sequence[ReportSummary]) -> None:
        # A list not yet mounted lists the shelf's reports as it's composed.
        screen = self._slot.showing
        if screen is not None and screen.is_mounted:
            screen.show(reports)

    def show_report(self, report: ReportShown) -> None:
        self._push(ReportScreen(report), _closed)


async def _closed(result: None) -> None:
    """Nothing follows a reports modal closing."""
