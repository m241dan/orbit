"""The explorer over the server host's directories, to add a workspace from or open one."""

import posixpath
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orbit.protocol import AddWorkspace, DirectoryEntry, DirectoryListing, ListDirectory
from orbit.tui.view.actions import action
from orbit.tui.view.common import kept_highlight
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.workspaces import add_command

EXPLORER_HINT = "j/k move · l/enter open · h up · a add · . hidden · / type · esc back"
FIELD_HINT = "tab complete · enter add · esc back"
REPOSITORY_MARK = "⎇"
SERVED_MARK = "(served)"


def entry_label(entry: DirectoryEntry) -> str:
    """What the explorer shows for ``entry``: its name, marked ``⎇`` if it's a repository,
    ``(served)`` if it's a workspace served, and ``→ <target>`` if it's a symlink."""
    parts = [f"{REPOSITORY_MARK} {entry.name}" if entry.is_repository else entry.name]
    if entry.workspace is not None:
        parts.append(SERVED_MARK)
    if entry.target is not None:
        parts.append(f"→ {entry.target}")
    return " ".join(parts)


def shown_entries(entries: Sequence[DirectoryEntry], hidden: bool) -> tuple[DirectoryEntry, ...]:
    """The ``entries`` the explorer shows: all of them while ``hidden`` ones are shown, else
    those that aren't hidden."""
    return tuple(entry for entry in entries if hidden or not entry.hidden)


def entry_path(listing: DirectoryListing, entry: DirectoryEntry) -> str:
    """The path of ``entry`` in the directory ``listing`` lists."""
    return posixpath.join(listing.path, entry.name)


def added_path(listing: DirectoryListing, entry: DirectoryEntry) -> str:
    """The path adding ``entry`` in the directory ``listing`` lists sends: a symlink's target,
    from the listing's path when it's relative, with ``..`` and ``.`` collapsed, so it's the
    path the server serves the workspace at; else the entry's path. A target that's itself a
    symlink can't be resolved here, as the server will."""
    if entry.target is None:
        return entry_path(listing, entry)
    return posixpath.normpath(posixpath.join(listing.path, entry.target))


def more_note(listing: DirectoryListing) -> str | None:
    """What says how many directories ``listing`` leaves out, when it's truncated."""
    if not listing.truncated:
        return None
    left = listing.total - len(listing.entries)
    return f"… {left} more not shown: type the path in the field"


def explorer_title(listing: DirectoryListing | None) -> str:
    """The explorer's title: the path of the directory ``listing`` lists, empty before one."""
    return "" if listing is None else listing.path


def answers(asked: str | None, listing: DirectoryListing) -> bool:
    """Whether ``listing`` answers listing the directory at ``asked`` by its path; any listing
    answers listing the starting directory, ``None``."""
    return asked is None or listing.path == asked


@dataclass(frozen=True)
class Completion:
    """A path completed: the field's ``text``, and the ``candidates`` it may go on to while
    there are several."""

    text: str
    candidates: tuple[str, ...] = ()


def completion_parent(text: str) -> str | None:
    """The directory whose entries complete the last component of the path ``text``: what's
    before its last ``/``, or ``/`` itself; none for text that isn't an absolute path."""
    if not text.startswith("/"):
        return None
    return posixpath.dirname(text)


def completed(text: str, names: Sequence[str]) -> Completion | None:
    """``text`` with its last component completed from the directory ``names`` it starts: the
    one match with a trailing ``/``, or the longest prefix several share, with them as
    candidates; none when nothing matches."""
    stem = posixpath.basename(text)
    matches = tuple(name for name in names if name.startswith(stem))
    if not matches:
        return None
    head = text[: len(text) - len(stem)]
    if len(matches) == 1:
        return Completion(f"{head}{matches[0]}/")
    return Completion(head + posixpath.commonprefix(matches), matches)


def candidates_line(candidates: Sequence[str]) -> str:
    """What shows the ``candidates`` a path may be completed to, under the field."""
    return "  ".join(candidates)


Browse = Callable[[ListDirectory | AddWorkspace, Awaited], Awaitable[object]]
"""Sends listing a directory or adding a workspace to the backend, awaiting its outcome as
told; whatever it returns is ignored."""


class ExplorerView(Protocol):
    """What the explorer shows and what the user has done in it: the widgets, which keep the
    highlight and the text typed, as the user changes them."""

    @property
    def is_attached(self) -> bool:
        """Whether the explorer is still open."""
        ...

    @property
    def is_active(self) -> bool:
        """Whether no other screen is over the explorer."""
        ...

    @property
    def highlighted(self) -> int | None:
        """The row highlighted, if any."""
        ...

    @property
    def field(self) -> str:
        """The text in the path or URL field."""
        ...

    def show_heading(self, title: str, more: str | None) -> None:
        """Show ``title`` over the listing and ``more`` under it, hiding that line for none."""
        ...

    def show_rows(self, keys: Sequence[str], labels: Sequence[str], highlight: int | None) -> None:
        """List ``labels``, the rows with ``keys``, highlighting row ``highlight``, if any."""
        ...

    def show_note(self, note: str | None) -> None:
        """Show ``note`` under the listing, or hide that line for none."""
        ...

    def set_field(self, text: str) -> None:
        """Put ``text`` in the field, the cursor at its end."""
        ...

    def show_field_note(self, note: str) -> None:
        """Show ``note`` in the field's hint, empty for none."""
        ...

    def show_candidates(self, candidates: Sequence[str]) -> None:
        """Show the ``candidates`` under the field, hiding that line for none."""
        ...

    def dismiss(self, result: str | AddWorkspace | None) -> object:
        """Close the explorer with ``result``."""
        ...


class Explorer:
    """The explorer over the server's directories, shown in ``view``, listing them and adding
    workspaces with ``browse``: it starts where the server starts, and closes with the id of a
    workspace to open, or the ``AddWorkspace`` accepted.

    Only the listing last asked for is shown, and a refusal shows why under it. In the field,
    the path or URL typed is added the same way, a refusal showing in the field's hint; tab
    completes the path's last component from the directories of its parent, from the listing
    shown when it's that directory's, else from a listing asked for alone, which isn't shown.
    """

    def __init__(self, view: ExplorerView, browse: Browse) -> None:
        self._view = view
        self._browse = browse
        self.listing: DirectoryListing | None = None  # The listing shown, if any.
        self.hidden = False  # Whether hidden directories are shown.
        self._asked: str | None = None  # The path last asked for, None for the start.
        self._waiting = False  # Whether the listing last asked for is yet to be shown.
        self._held: DirectoryListing | None = None  # A listing for another path, meanwhile.
        self._completing: str | None = None  # The parent typed, listed to complete from.
        self._completion: DirectoryListing | None = None  # The listing come for it, meanwhile.
        self._requests = 0  # How many listings have been asked for to complete from.
        self._accepted: AddWorkspace | None = None  # An add accepted under another screen.

    @property
    def entries(self) -> tuple[DirectoryEntry, ...]:
        """The directories shown, in order."""
        entries = () if self.listing is None else self.listing.entries
        return shown_entries(entries, self.hidden)

    @property
    def paths(self) -> list[str]:
        """The paths of the directories shown, in order, which key their rows."""
        return [self._path_of(entry) for entry in self.entries]

    async def start(self) -> None:
        """List the directory the server starts at."""
        await self._list(None)

    async def _list(self, path: str | None) -> None:
        """Ask for the listing of the directory at ``path``, or the starting one for none."""
        self._asked = path
        self._waiting = True
        self._held = None
        self._view.show_note(None)
        awaited = Awaited(accepted=self._listed, why=self._unlisted, supersedes=True)
        with action("browse"):
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
        """Show the listing held now the one last asked for is accepted, if it's not shown,
        unless the explorer is closed."""
        if not self._view.is_attached:
            return
        held = self._held
        if self._waiting and held is not None:
            self._show(held)
        self._waiting = False

    def _show(self, listing: DirectoryListing) -> None:
        """Show ``listing`` in place of the one shown, highlighting its first directory."""
        self.listing = listing
        self._waiting = False
        self._held = None
        self._view.show_heading(explorer_title(listing), more_note(listing))
        self._relist((), None)  # A new listing starts at its top.

    def _relist(self, shown: Sequence[str], highlighted: int | None) -> None:
        """List the directories shown in place of those at the paths ``shown``, of which row
        ``highlighted`` was highlighted, keeping the highlight on its directory, or where it
        was if that one's gone, or else on the first."""
        entries = self.entries
        highlight = kept_highlight(shown, highlighted, self.paths)
        if highlight is None and entries:
            highlight = 0
        self._view.show_rows(self.paths, [entry_label(entry) for entry in entries], highlight)

    def _unlisted(self, why: str) -> None:
        """Show ``why`` the listing last asked for was refused, keeping the one shown."""
        self._waiting = False
        self._held = None
        self._refused(why)

    def _refused(self, why: str) -> None:
        """Show ``why`` a change was refused under the listing, unless the explorer is closed."""
        if self._view.is_attached:
            self._view.show_note(why)

    @property
    def _highlighted(self) -> DirectoryEntry | None:
        """The directory highlighted, if any."""
        index = self._view.highlighted
        return None if index is None else self.entries[index]

    def _path_of(self, entry: DirectoryEntry) -> str:
        """The path of ``entry``, in the listing shown."""
        assert self.listing is not None  # Entries are only shown with their listing.
        return entry_path(self.listing, entry)

    async def choose(self, index: int) -> None:
        """Open the workspace served at row ``index``, or else list its directory."""
        entry = self.entries[index]
        if entry.workspace is not None:
            self._view.dismiss(entry.workspace)
        else:
            await self._list(self._path_of(entry))

    async def into(self) -> None:
        """List the directory highlighted, if any."""
        entry = self._highlighted
        if entry is not None:
            await self._list(self._path_of(entry))

    async def up(self) -> None:
        """List the directory above the one shown, if there's one."""
        if self.listing is not None and self.listing.parent is not None:
            await self._list(self.listing.parent)

    async def add(self) -> None:
        """Add the directory highlighted, if any, at its target if it's a symlink, closing with
        it once accepted."""
        entry = self._highlighted
        if entry is None:
            return
        assert self.listing is not None  # Entries are only shown with their listing.
        adding = AddWorkspace(path=added_path(self.listing, entry))
        self._view.show_note(None)
        awaited = Awaited(accepted=lambda: self._added(adding), why=self._refused)
        with action("add_workspace"):
            await self._browse(adding, awaited)

    def _added(self, adding: AddWorkspace) -> None:
        """Close with ``adding`` now it's accepted, or once the explorer is on top again if
        another screen is over it, unless it's closed."""
        if self._view.is_attached:
            self._accepted = adding
            self.resumed()

    def resumed(self) -> None:
        """Close with the add accepted, if any, unless another screen is over the explorer."""
        adding = self._accepted
        if adding is None or not self._view.is_active:
            return
        self._accepted = None
        self._view.dismiss(adding)

    def toggle_hidden(self) -> None:
        """Show hidden directories, or hide them, keeping the highlight on its directory."""
        shown = self.paths
        self.hidden = not self.hidden
        self._relist(shown, self._view.highlighted)

    async def complete(self) -> None:
        """Complete the last component of the path typed, from the listing shown when it's its
        parent's, else from that parent's listing once it comes; nothing for no path."""
        parent = completion_parent(self._view.field)
        if parent is None:
            return
        self._view.show_field_note("")
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
        with action("browse"):
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
        self._show_candidates(())

    def _complete(self, listing: DirectoryListing, parent: str) -> None:
        """Complete the path typed from the directories ``listing`` shows, as those of the
        directory typed as ``parent``, unless it's been typed on into another directory since;
        show the candidates when there are several, and none otherwise."""
        typed = self._view.field
        if completion_parent(typed) != parent:
            self._show_candidates(())
            return
        names = [entry.name for entry in shown_entries(listing.entries, self.hidden)]
        completion = completed(typed, names)
        if completion is None:
            self._show_candidates(())
            return
        self._view.set_field(completion.text)
        self._show_candidates(completion.candidates)

    def _show_candidates(self, candidates: Sequence[str]) -> None:
        """Show the ``candidates`` under the field, hiding the line for none; nothing once the
        explorer is closed."""
        if self._view.is_attached:
            self._view.show_candidates(candidates)

    async def submit(self, text: str) -> None:
        """Add the path or URL typed, ``text``, if any, closing with it once accepted."""
        self._show_candidates(())
        text = text.strip()
        if not text:
            return
        adding = add_command(text)
        self._view.show_field_note("")
        awaited = Awaited(accepted=lambda: self._added(adding), why=self._refused_typed)
        with action("add_workspace"):
            await self._browse(adding, awaited)

    def _refused_typed(self, why: str) -> None:
        """Show ``why`` what was typed was refused in the field's hint; nothing once the
        explorer is closed."""
        if self._view.is_attached:
            self._view.show_field_note(why)
