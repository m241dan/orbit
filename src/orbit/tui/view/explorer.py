"""The explorer over the server host's directories, to add a workspace from or open one."""

import posixpath
from collections.abc import Sequence

from orbit.protocol import DirectoryEntry, DirectoryListing

EXPLORER_HINT = "j/k move · l/enter open · h up · a add · . hidden · esc back"
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
