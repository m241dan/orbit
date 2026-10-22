"""The explorer over the server host's directories, opened from the workspace picker."""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from orbit.protocol import AddWorkspace, DirectoryEntry, DirectoryListing, ListDirectory
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.explorer import (
    EXPLORER_HINT,
    answers,
    entry_label,
    entry_path,
    explorer_title,
    more_note,
    shown_entries,
)
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspaces import PATH_PLACEHOLDER
from orbit.tui.widgets import ListScreen

Browse = Callable[[ListDirectory | AddWorkspace, Awaited], Awaitable[object]]
"""Sends listing a directory or adding a workspace to the backend, awaiting its outcome as
told; whatever it returns is ignored."""


class ExplorerScreen(ListScreen[str | AddWorkspace | None]):
    """Lists the directories on the server's host, starting where the server starts, under the
    path or URL field, which takes no typing yet; dismisses with the id of a workspace to open,
    or the ``AddWorkspace`` accepted.

    ``j``/``k`` move; ``l`` or enter lists the highlighted directory, ``h`` the one above; enter
    on a workspace served opens it; ``a`` adds the highlighted directory with ``browse``,
    dismissing once it's accepted; ``.`` shows or hides hidden directories; escape dismisses
    with none. Only the listing last asked for is shown, and a refusal shows why under it.
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
    ExplorerScreen Input { border: round $secondary; }
    ExplorerScreen #explorer-note { color: $error; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("l", "into", show=False),
        Binding("h", "up", show=False),
        Binding("a", "add", show=False),
        Binding("full_stop", "hidden", show=False),
        Binding("escape", "back", show=False),
    ]

    def __init__(self, browse: Browse) -> None:
        super().__init__()
        self._browse = browse
        self.listing: DirectoryListing | None = None  # The listing shown, if any.
        self.hidden = False  # Whether hidden directories are shown.
        self._asked: str | None = None  # The path last asked for, None for the start.
        self._waiting = False  # Whether the listing last asked for is yet to be shown.
        self._held: DirectoryListing | None = None  # A listing for another path, meanwhile.

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = explorer_title(None)
            box.border_subtitle = EXPLORER_HINT
            field = Input(placeholder=PATH_PLACEHOLDER, id="explorer-path")
            field.can_focus = False
            yield field
            yield OptionList()
            more = Static("", id="explorer-more")
            more.display = False
            yield more
            note = Static("", id="explorer-note")
            note.display = False
            yield note

    async def on_mount(self) -> None:
        await self._list(None)

    @property
    def entries(self) -> tuple[DirectoryEntry, ...]:
        """The directories shown, in order."""
        entries = () if self.listing is None else self.listing.entries
        return shown_entries(entries, self.hidden)

    @property
    def note(self) -> str | None:
        """Why listing or adding was refused, while that's shown."""
        note = self.query_one("#explorer-note", Static)
        return str(note.render()) if note.display else None

    async def _list(self, path: str | None) -> None:
        """Ask for the listing of the directory at ``path``, or the starting one for none."""
        self._asked = path
        self._waiting = True
        self._held = None
        self._show_note(None)
        awaited = Awaited(accepted=self._listed, why=self._unlisted)
        await self._browse(ListDirectory(path=path), awaited)

    def show_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` if it answers the listing last asked for, by its path; hold one for
        another path, as a symlink followed gives, until that listing is accepted."""
        if not self._waiting:
            return
        if answers(self._asked, listing):
            self._show(listing)
        else:
            self._held = listing

    def _listed(self) -> None:
        """Show the listing held now the one last asked for is accepted, if it's not shown."""
        held = self._held
        if self._waiting and held is not None:
            self._show(held)
        self._waiting = False

    def _show(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in place of the one shown, highlighting its first directory."""
        self.listing = listing
        self._waiting = False
        self._held = None
        self.query_one(Vertical).border_title = explorer_title(listing)
        more = more_note(listing)
        shown = self.query_one("#explorer-more", Static)
        shown.update(more or "")
        shown.display = more is not None
        self._relist(0)

    def _relist(self, highlighted: int | None) -> None:
        """List the directories shown, highlighting the row at ``highlighted``."""
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options([entry_label(entry) for entry in self.entries])
        options.highlighted = highlighted if self.entries else None

    def _unlisted(self, why: str) -> None:
        """Show ``why`` the listing last asked for was refused, keeping the one shown."""
        self._waiting = False
        self._held = None
        self._refused(why)

    def _refused(self, why: str) -> None:
        """Show ``why`` listing or adding was refused under the listing, which stays; nothing
        once this screen is gone."""
        if self.is_attached:
            self._show_note(why)

    @property
    def _highlighted(self) -> DirectoryEntry | None:
        """The directory highlighted, if any."""
        index = self.query_one(OptionList).highlighted
        return None if index is None else self.entries[index]

    def _path_of(self, entry: DirectoryEntry) -> str:
        """The path of ``entry``, in the listing shown."""
        assert self.listing is not None  # Entries are only shown with their listing.
        return entry_path(self.listing, entry)

    @on(OptionList.OptionSelected)
    async def _chosen(self, event: OptionList.OptionSelected) -> None:
        entry = self.entries[event.option_index]
        if entry.workspace is not None:
            self.dismiss(entry.workspace)
        else:
            await self._list(self._path_of(entry))

    async def action_into(self) -> None:
        """List the directory highlighted, if any."""
        entry = self._highlighted
        if entry is not None:
            await self._list(self._path_of(entry))

    async def action_up(self) -> None:
        """List the directory above the one shown, if there's one."""
        if self.listing is not None and self.listing.parent is not None:
            await self._list(self.listing.parent)

    async def action_add(self) -> None:
        """Add the directory highlighted, if any, dismissing with it once accepted."""
        entry = self._highlighted
        if entry is None:
            return
        adding = AddWorkspace(path=self._path_of(entry))
        self._show_note(None)
        await self._browse(adding, Awaited(accepted=lambda: self._added(adding), why=self._refused))

    def _added(self, adding: AddWorkspace) -> None:
        """Dismiss with ``adding`` now it's accepted, unless this screen is gone."""
        if self.is_attached:
            self.dismiss(adding)

    def action_hidden(self) -> None:
        """Show hidden directories, or hide them, keeping the highlight on its directory."""
        shown = [entry.name for entry in self.entries]
        highlighted = self.query_one(OptionList).highlighted
        self.hidden = not self.hidden
        kept = kept_highlight(shown, highlighted, [entry.name for entry in self.entries])
        self._relist(kept or 0)

    def action_back(self) -> None:
        self.dismiss(None)

    def _show_note(self, note: str | None) -> None:
        """Show ``note`` under the listing, or hide it while it's none."""
        shown = self.query_one("#explorer-note", Static)
        shown.update(note or "")
        shown.display = note is not None
