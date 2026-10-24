"""The explorer over the server host's directories, opened from the workspace picker."""

from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from orbit.protocol import AddWorkspace, DirectoryEntry, DirectoryListing, ListDirectory
from orbit.tui.view.explorer import (
    EXPLORER_HINT,
    FIELD_HINT,
    added_path,
    answers,
    candidates_line,
    completed,
    completion_parent,
    entry_label,
    entry_path,
    explorer_title,
    more_note,
    shown_entries,
)
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspaces import PATH_PLACEHOLDER, add_command
from orbit.tui.widgets import ListScreen, PromptInput

Browse = Callable[[ListDirectory | AddWorkspace, Awaited], Awaitable[object]]
"""Sends listing a directory or adding a workspace to the backend, awaiting its outcome as
told; whatever it returns is ignored."""


class ExplorerField(PromptInput):
    """The explorer's path or URL field, which only takes focus once asked to; tab completes
    its path and escape returns to the listing."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "screen.leave_field", show=False),
        Binding("tab", "screen.complete", show=False),
    ]


class ExplorerScreen(ListScreen[str | AddWorkspace | None]):
    """Lists the directories on the server's host, starting where the server starts, under the
    path or URL field; dismisses with the id of a workspace to open, or the ``AddWorkspace``
    accepted.

    ``j``/``k`` move; ``l`` or enter lists the highlighted directory, ``h`` the one above; enter
    on a workspace served opens it; ``a`` adds the highlighted directory, or a symlink's target,
    with ``browse``, dismissing once it's accepted; ``.`` shows or hides hidden directories;
    ``/`` types in the field; escape dismisses with none. Only the listing last asked for is
    shown, and a refusal shows why under it.

    In the field, enter adds the path or URL typed, the same way, showing a refusal in the
    field's hint; tab completes the path's last component from the directories of its parent,
    from the listing shown when it's that directory's, else from a listing asked for alone,
    which isn't shown; escape returns to the listing.
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
    ExplorerScreen Input:focus { border: heavy $accent; }
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
        self._browse = browse
        self.listing: DirectoryListing | None = None  # The listing shown, if any.
        self.hidden = False  # Whether hidden directories are shown.
        self._asked: str | None = None  # The path last asked for, None for the start.
        self._waiting = False  # Whether the listing last asked for is yet to be shown.
        self._held: DirectoryListing | None = None  # A listing for another path, meanwhile.
        self._completing: str | None = None  # The parent typed, listed to complete from.
        self._completion: DirectoryListing | None = None  # The listing come for it, meanwhile.
        self._requests = 0  # How many listings have been asked for to complete from.

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
            more = Static("", id="explorer-more")
            more.display = False
            yield more
            yield self._note_line()

    async def on_mount(self) -> None:
        await self._list(None)

    @property
    def entries(self) -> tuple[DirectoryEntry, ...]:
        """The directories shown, in order."""
        entries = () if self.listing is None else self.listing.entries
        return shown_entries(entries, self.hidden)

    async def _list(self, path: str | None) -> None:
        """Ask for the listing of the directory at ``path``, or the starting one for none."""
        self._asked = path
        self._waiting = True
        self._held = None
        self._show_note(None)
        awaited = Awaited(accepted=self._listed, why=self._unlisted, supersedes=True)
        await self._browse(ListDirectory(path=path), awaited)

    def show_listing(self, listing: DirectoryListing) -> None:
        """Show ``listing`` if it answers the listing last asked for, by its path; else hold it
        for the listing asked for to complete from, if one is, as that one may have its symlinks
        followed, or else for the listing last asked for, as a symlink followed gives, until
        that one's accepted."""
        answered = self._waiting and answers(self._asked, listing)
        held = self._held_to_complete(listing, answered)
        if answered:
            self._show(listing)
        elif self._waiting and not held:
            self._held = listing

    def _held_to_complete(self, listing: DirectoryListing, answered: bool) -> bool:
        """Whether ``listing`` is held to complete from, as it is while a listing's asked for
        to complete from unless it's ``answered`` the one last asked for, for another path."""
        if self._completing is None:
            return False
        if answered and listing.path != self._completing:
            return False
        self._completion = listing
        return True

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
        self.query_one(OptionList).highlighted = None  # A new listing starts at its top.
        self._relist_entries(())

    def _relist_entries(self, shown: Sequence[str]) -> None:
        """List the directories shown in place of those at the paths ``shown``, keeping the
        highlight on its directory, or where it was if that one's gone, or else on the first."""
        entries = self.entries
        self._relist(shown, self._paths, [entry_label(entry) for entry in entries])
        options = self.query_one(OptionList)
        if options.highlighted is None and entries:
            options.highlighted = 0

    def _unlisted(self, why: str) -> None:
        """Show ``why`` the listing last asked for was refused, keeping the one shown."""
        self._waiting = False
        self._held = None
        self._refused(why)

    @property
    def _highlighted(self) -> DirectoryEntry | None:
        """The directory highlighted, if any."""
        index = self.query_one(OptionList).highlighted
        return None if index is None else self.entries[index]

    @property
    def _paths(self) -> list[str]:
        """The paths of the directories shown, in order."""
        return [self._path_of(entry) for entry in self.entries]

    def _path_of(self, entry: DirectoryEntry) -> str:
        """The path of ``entry``, in the listing shown."""
        assert self.listing is not None  # Entries are only shown with their listing.
        return entry_path(self.listing, entry)

    @on(OptionList.OptionSelected)
    async def _chosen(self, event: OptionList.OptionSelected) -> None:
        index = self._picked_row(event, self._paths)
        if index is None:
            return
        entry = self.entries[index]
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
        """Add the directory highlighted, if any, at its target if it's a symlink, dismissing
        with it once accepted."""
        entry = self._highlighted
        if entry is None:
            return
        assert self.listing is not None  # Entries are only shown with their listing.
        adding = AddWorkspace(path=added_path(self.listing, entry))
        self._show_note(None)
        await self._browse(adding, Awaited(accepted=lambda: self._added(adding), why=self._refused))

    def _added(self, adding: AddWorkspace) -> None:
        """Dismiss with ``adding`` now it's accepted, unless this screen is gone."""
        if self.is_attached:
            self.dismiss(adding)

    def action_hidden(self) -> None:
        """Show hidden directories, or hide them, keeping the highlight on its directory."""
        shown = self._paths
        self.hidden = not self.hidden
        self._relist_entries(shown)

    def action_back(self) -> None:
        self.dismiss(None)

    def action_type(self) -> None:
        """Type in the field."""
        self.query_one(ExplorerField).enter_insert_mode()

    def action_leave_field(self) -> None:
        """Return from the field to the listing."""
        self.query_one(OptionList).focus()

    async def action_complete(self) -> None:
        """Complete the last component of the path typed, from the listing shown when it's its
        parent's, else from that parent's listing once it comes; nothing for no path."""
        parent = completion_parent(self.query_one(ExplorerField).value)
        if parent is None:
            return
        if self.listing is not None and self.listing.path == parent:
            self._complete(self.listing, parent)
            return
        self._requests += 1
        request = self._requests
        self._completing = parent
        self._completion = None
        awaited = Awaited(
            accepted=lambda: self._completed(request),
            why=lambda why: self._uncompleted(request, why),
        )
        await self._browse(ListDirectory(path=parent), awaited)

    def _completed(self, request: int) -> None:
        """Complete the path typed from the listing come for the ``request``, the number of the
        listing asked for to complete from, once it's accepted, unless another's asked for."""
        parent, listing = self._completing, self._completion
        if request != self._requests or parent is None:
            return
        self._completing = None
        self._completion = None
        if listing is not None:
            self._complete(listing, parent)

    def _uncompleted(self, request: int, why: str) -> None:
        """Show ``why`` the ``request``, the number of the listing asked for to complete from,
        was refused, leaving nothing to complete from unless another's asked for."""
        if request == self._requests:
            self._completing = None
            self._completion = None
        self._refused_typed(why)

    def _complete(self, listing: DirectoryListing, parent: str) -> None:
        """Complete the path typed from the directories ``listing`` shows, as those of the
        directory typed as ``parent``, unless it's been typed on into another directory since;
        show the candidates when there are several."""
        field = self.query_one(ExplorerField)
        if completion_parent(field.value) != parent:
            return
        names = [entry.name for entry in shown_entries(listing.entries, self.hidden)]
        completion = completed(field.value, names)
        if completion is None:
            return
        field.value = completion.text
        field.cursor_position = len(completion.text)
        shown = self.query_one("#explorer-candidates", Static)
        shown.update(candidates_line(completion.candidates))
        shown.display = bool(completion.candidates)

    @on(Input.Submitted, "#explorer-path")
    async def _typed(self, event: Input.Submitted) -> None:
        """Add the path or URL typed, if any, dismissing with it once accepted."""
        event.stop()
        text = event.value.strip()
        if not text:
            return
        adding = add_command(text)
        field = self.query_one(ExplorerField)
        field.border_subtitle = ""
        awaited = Awaited(accepted=lambda: self._added(adding), why=self._refused_typed)
        await self._browse(adding, awaited)

    def _refused_typed(self, why: str) -> None:
        """Show ``why`` what was typed was refused in the field's hint; nothing once this screen
        is gone."""
        if self.is_attached:
            self.query_one(ExplorerField).border_subtitle = why
