"""Fakes for the conversation's models: the list a tab's entries show in, and what it shows."""

from collections.abc import Callable, Sequence

from orbit.tui.view.log import EntryLog, EntryRow


class EntryList:
    """A tab's list, as a log's entries show in it: a row per entry shown, the cursor the user
    moves, whether it's active, how many times the log was rebuilt, scrolled to its end, and
    scrolled to its place once laid out, and how many times it said the detail pane may need
    to change."""

    def __init__(self, thinking_hidden: bool = False, clock: Callable[[], float] = lambda: 0.0):
        self.highlighted: int | None = None
        self.has_focus = False
        self.rows: list[EntryRow] = []
        self.rebuilds = 0
        self.ends = 0
        self.places = 0
        self.inspects = 0
        self.entry_log = EntryLog(self, thinking_hidden, clock)

    def move(self, row: int | None) -> None:
        """Put the cursor on option ``row``, as the user moves it, the log moving its marker as
        the list tells it whenever the cursor changes."""
        if row != self.highlighted:
            self.highlighted = row
            self.entry_log.moved(row)

    def highlight(self, row: int | None) -> None:
        self.move(row)

    def add_row(self, row: EntryRow) -> None:
        self.rows.append(row)

    def redraw(self, index: int, row: EntryRow) -> None:
        self.rows[index] = row

    def replace_rows(self, rows: Sequence[EntryRow], highlight: int | None) -> None:
        self.rebuilds += 1
        self.clear_rows()
        self.rows = list(rows)
        self.move(highlight)
        self.show_place()

    def clear_rows(self) -> None:
        self.rows = []
        self.move(None)

    def scroll_to_end(self) -> None:
        self.ends += 1

    def show_place(self) -> None:
        self.places += 1

    def detail_changed(self) -> None:
        self.inspects += 1

    @property
    def labels(self) -> list[str]:
        """The rows' labels, without their gutter."""
        return [row.label for row in self.rows]

    @property
    def gutters(self) -> list[str]:
        """The gutter symbol of each row."""
        return [row.gutter.strip() for row in self.rows]

    @property
    def headings(self) -> list[str | None]:
        """The label row heading each row, if it has one."""
        return [row.heading for row in self.rows]
