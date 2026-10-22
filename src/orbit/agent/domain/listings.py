"""What frontends browsing the server's host are shown: a directory's listing, its directories
in order and capped, and which paths they may list or add."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID, UNKNOWN, DirectoryEntry, DirectoryListing

MAX_ENTRIES = 2000
"""The most directories a listing holds."""

LOCAL_BROWSING = "A local orbit works on one repository; run orbit serve to browse for others."
"""Why a local orbit refuses listing directories."""


@dataclass(frozen=True)
class FoundDirectory:
    """A directory ``name``d in the one listed, at ``path`` once its symlinks are followed,
    whether it ``is_repository``, holding ``.git``, and, for a symlink, its ``target`` as the
    link gives it."""

    name: str
    path: str
    is_repository: bool
    target: str | None = None


def check_listable(path: str, *, absolute: bool) -> None:
    """Refuse listing the directory at ``path`` as ``invalid`` unless it's ``absolute``."""
    if not absolute:
        raise Refused(INVALID, f"Can't list {path}: give the absolute path to a directory.")


def check_inside(path: str, root: str | None, *, inside: bool) -> None:
    """Refuse browsing or adding ``path`` as ``invalid`` unless it's ``inside`` ``root``, once
    its symlinks are followed, if there is one."""
    if root is not None and not inside:
        raise Refused(INVALID, f"{path} is outside the browsable root {root}.")


def unreadable(path: str, error: OSError) -> Refused:
    """The refusal of listing the directory at ``path``, which reading failed with ``error``:
    ``unknown`` when there's nothing there, else ``invalid``."""
    if isinstance(error, FileNotFoundError):
        return Refused(UNKNOWN, f"Can't list {path}: there's nothing there.")
    if isinstance(error, PermissionError):
        return Refused(INVALID, f"Permission denied: {path}")
    if isinstance(error, NotADirectoryError):
        return Refused(INVALID, f"Can't list {path}: it isn't a directory.")
    return Refused(INVALID, f"Can't list {path}: {error.strerror or error}")


def listing(
    path: str, parent: str | None, found: Sequence[FoundDirectory], served: Mapping[str, str]
) -> DirectoryListing:
    """The listing of the directory at ``path``, below ``parent``, if it may be browsed,
    holding the directories ``found`` there, repositories first, then by name, at most
    :data:`MAX_ENTRIES` of them, each marked with the id of the workspace ``served`` at its
    path, if any."""
    ordered = sorted(found, key=_order)[:MAX_ENTRIES]
    return DirectoryListing(
        path=path,
        parent=parent,
        entries=tuple(_entry(directory, served) for directory in ordered),
        truncated=len(found) > MAX_ENTRIES,
        total=len(found),
    )


def _order(directory: FoundDirectory) -> tuple[bool, str, str]:
    """Where ``directory`` comes in a listing: repositories first, then by name, ignoring case
    and a leading ``.``."""
    return (not directory.is_repository, directory.name.lstrip(".").casefold(), directory.name)


def _entry(directory: FoundDirectory, served: Mapping[str, str]) -> DirectoryEntry:
    """``directory``'s entry in a listing, with the id of the workspace ``served`` at its path,
    if any."""
    return DirectoryEntry(
        name=directory.name,
        is_repository=directory.is_repository,
        workspace=served.get(directory.path),
        hidden=directory.name.startswith("."),
        target=directory.target,
    )
