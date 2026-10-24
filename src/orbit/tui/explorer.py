"""The explorer over the server host's directories, opened from the workspace picker."""

from collections.abc import Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from orbit.protocol import AddWorkspace, DirectoryListing
from orbit.tui.view.explorer import (
    EXPLORER_HINT,
    FIELD_HINT,
    Browse,
    Explorer,
    candidates_line,
    explorer_title,
)
from orbit.tui.view.workspaces import PATH_PLACEHOLDER
from orbit.tui.widgets import ListScreen, NoteLine, PromptInput


class ExplorerField(PromptInput):
    """The explorer's path or URL field, which only takes focus once asked to; tab completes
    its path and escape returns to the listing."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "screen.leave_field", show=False),
        Binding("tab", "screen.complete", show=False),
    ]


class ExplorerScreen(ListScreen[str | AddWorkspace | None]):
    """Shows the :class:`Explorer` over the server's directories, dismissing with what it closes
    with: the id of a workspace to open, or the ``AddWorkspace`` accepted.

    ``j``/``k`` move; ``l`` or enter lists the highlighted directory, ``h`` the one above; enter
    on a workspace served opens it; ``a`` adds the highlighted directory; ``.`` shows or hides
    hidden directories; ``/`` types in the field; escape dismisses with none. In the field,
    enter adds the path or URL typed, tab completes it and escape returns to the listing.
    """

    AUTO_FOCUS = "OptionList"
    DEFAULT_CSS = """
    ExplorerScreen { align: center middle; }
    ExplorerScreen > Vertical {
        width: 90;
        max-width: 90%;
        height: 100%;
        border: round $accent;
        padding: 0 1;
    }
    ExplorerScreen OptionList { height: 1fr; border: none; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("l", "into", show=False),
        Binding("h", "up", show=False),
        Binding("a", "add", show=False),
        Binding("full_stop", "hidden", show=False),
        Binding("slash", "type", show=False),
        Binding("escape", "back", show=False),
    ]

    def __init__(self, browse: Browse) -> None:
        super().__init__()
        self.explorer = Explorer(self, browse)

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = explorer_title(None)
            box.border_subtitle = EXPLORER_HINT
            field = ExplorerField(placeholder=PATH_PLACEHOLDER, id="explorer-path")
            field.border_title = FIELD_HINT
            yield field
            candidates = Static("", id="explorer-candidates")
            candidates.display = False
            yield candidates
            yield OptionList()
            yield NoteLine("", id="explorer-more")
            yield self._note_line()

    async def on_mount(self) -> None:
        await self.explorer.start()

    def show_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing``, as the explorer takes it."""
        self.explorer.show_listing(listing)

    @property
    def highlighted(self) -> int | None:
        return self.query_one(OptionList).highlighted

    @property
    def field(self) -> str:
        return self.query_one(ExplorerField).value

    def show_heading(self, title: str, more: str | None) -> None:
        self.query_one(Vertical).border_title = title
        self.query_one("#explorer-more", NoteLine).show(more)

    def show_rows(self, keys: Sequence[str], labels: Sequence[str], highlight: int | None) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(self._options(keys, labels))
        options.highlighted = highlight

    def show_note(self, note: str | None) -> None:
        self._show_note(note)

    def set_field(self, text: str) -> None:
        field = self.query_one(ExplorerField)
        field.value = text
        field.cursor_position = len(text)

    def show_field_note(self, note: str) -> None:
        self.query_one(ExplorerField).border_subtitle = note

    def show_candidates(self, candidates: Sequence[str]) -> None:
        shown = self.query_one("#explorer-candidates", Static)
        shown.update(candidates_line(candidates))
        shown.display = bool(candidates)

    @on(OptionList.OptionSelected)
    async def _chosen(self, event: OptionList.OptionSelected) -> None:
        index = self._picked_row(event, self.explorer.paths)
        if index is not None:
            await self.explorer.choose(index)

    async def action_into(self) -> None:
        await self.explorer.into()

    async def action_up(self) -> None:
        await self.explorer.up()

    async def action_add(self) -> None:
        await self.explorer.add()

    def on_screen_resume(self) -> None:
        self.explorer.resumed()

    def action_hidden(self) -> None:
        self.explorer.toggle_hidden()

    def action_back(self) -> None:
        self.dismiss(None)

    def action_type(self) -> None:
        """Type in the field."""
        self.query_one(ExplorerField).enter_insert_mode()

    def action_leave_field(self) -> None:
        """Return from the field to the listing."""
        self.query_one(OptionList).focus()

    async def action_complete(self) -> None:
        await self.explorer.complete()

    @on(Input.Submitted, "#explorer-path")
    async def _typed(self, event: Input.Submitted) -> None:
        event.stop()
        await self.explorer.submit(event.value)
