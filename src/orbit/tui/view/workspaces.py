"""The list of workspaces a control plane serves, to pick one from, add one or remove one."""

import posixpath
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from orbit.protocol import (
    AddWorkspace,
    RemoveWorkspace,
    SessionStateChanged,
    WorkspaceCloning,
    WorkspaceEntry,
    with_state,
)
from orbit.tui.view.common import ListHints, active_session

WORKSPACES_TITLE = "Workspaces"
ADD_PATH: Literal["add_path"] = "add_path"
ADD_PATH_LABEL = "Add a path or URL…"
PATH_PLACEHOLDER = "Absolute path on the server's host, or a repository's URL to clone"
READ_ONLY_NOTE = "read-only"
REMOVE_MESSAGE = "Its sessions can be kept, for when it's added again, or deleted with it."
KEEP_CHOICE = "Keep its sessions"
PURGE_CHOICE = "Delete its sessions too"
CANCEL_CHOICE = "Cancel"
CANCEL_INDEX = 2
LIST_HINT = "j/k move · enter open · x remove · esc close"
STARTUP_HINT = "j/k move · enter open · x remove · esc quit"
PATH_HINT = "enter add · esc back"
WORKSPACES_HINTS = ListHints(closing=LIST_HINT, quitting=STARTUP_HINT)

# What a repository's URL starts with, as against a path: a scheme git clones over, or the
# user and host of a scp-style ``user@host:path``.
_URL = re.compile(r"(?:https|ssh|file)://|[^\s/@:]+@[^\s/:]+:")
# What a clone's directory name can't hold, as the server names it.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

WorkspaceRow = WorkspaceEntry | AddWorkspace | WorkspaceCloning | Literal["add_path"]
"""A row of the picker: a workspace served, adding the repository the frontend runs in, a clone
under way, or adding a path or URL typed in."""


def workspace_rows(
    workspaces: Sequence[WorkspaceEntry],
    repository: str | None,
    cloning: Sequence[WorkspaceCloning] = (),
) -> tuple[WorkspaceRow, ...]:
    """The rows the picker lists: adding the ``repository`` the frontend runs in first, if
    there's one and none of ``workspaces`` is at it, then ``workspaces``, then the clones
    ``cloning``, then adding a path or URL."""
    served = workspace_at(workspaces, repository) is not None
    first = [] if repository is None or served else [AddWorkspace(path=repository)]
    return (*first, *workspaces, *cloning, ADD_PATH)


def add_command(text: str) -> AddWorkspace:
    """Adding what the user typed, ``text``: by its ``url`` if it starts as a repository's URL
    does (``https://``, ``ssh://``, ``file://`` or a scp-style ``user@host:path``), else by its
    ``path``."""
    if _URL.match(text):
        return AddWorkspace(url=text)
    return AddWorkspace(path=text)


def workspace_at(
    workspaces: Sequence[WorkspaceEntry], repository: str | None
) -> WorkspaceEntry | None:
    """The workspace among ``workspaces`` at the ``repository`` the frontend runs in; none
    without a repository, even for a workspace without a path."""
    if repository is None:
        return None
    return next((entry for entry in workspaces if entry.path == repository), None)


def preselected(rows: Sequence[WorkspaceRow], repository: str | None) -> int:
    """The index of the row of the workspace at the ``repository`` the frontend runs in among
    ``rows``, else the first's."""
    entries = [row for row in rows if isinstance(row, WorkspaceEntry)]
    entry = workspace_at(entries, repository)
    return 0 if entry is None else rows.index(entry)


def row_key(row: WorkspaceRow) -> str:
    """What tells ``row`` apart from the picker's other rows, as its workspace's sessions
    change."""
    if isinstance(row, AddWorkspace):
        return f"add {row.path}"
    if isinstance(row, WorkspaceCloning):
        return f"cloning {row.url}"
    return row.id if isinstance(row, WorkspaceEntry) else row


def row_label(row: WorkspaceRow) -> str:
    """What the picker shows for ``row``."""
    if isinstance(row, AddWorkspace):
        return f"Add {row.path}"
    if isinstance(row, WorkspaceEntry):
        return workspace_label(row)
    if isinstance(row, WorkspaceCloning):
        return clone_label(row)
    return ADD_PATH_LABEL


def workspace_label(entry: WorkspaceEntry) -> str:
    """A workspace's row: its label, its path, its active session's name and state, and
    ``read-only`` while another orbit owns it: ``orbit · /src/orbit · today, idle``."""
    details = [entry.label] if entry.path is None else [entry.label, entry.path]
    active = active_session(entry.sessions)
    if active is not None:
        details.append(f"{active.name}, {active.state}")
    if entry.read_only:
        details.append(READ_ONLY_NOTE)
    return " · ".join(details)


def clone_label(cloning: WorkspaceCloning) -> str:
    """A clone's row: its name and how far through its phase git is, or the phase while git
    doesn't say: ``Cloning proj… 42%``."""
    progress = cloning.phase if cloning.percent is None else f"{cloning.percent}%"
    return f"Cloning {clone_name(cloning.url)}… {progress}"


def clone_name(url: str) -> str:
    """The name the server gives the directory a clone of ``url`` goes to: its last part,
    without ``.git``, with what isn't a letter, digit, dot, dash or underscore made a dash, and
    no leading or trailing dots or dashes; ``repository`` if nothing's left."""
    last = re.split(r"[/:]", url.rstrip("/"))[-1]
    name = _UNSAFE.sub("-", last.removesuffix(".git")).strip(".-")
    return name or "repository"


def with_clone(
    cloning: Sequence[WorkspaceCloning], progress: WorkspaceCloning
) -> tuple[WorkspaceCloning, ...]:
    """The clones ``cloning``, with ``progress`` in place of the last of its URL's, or after
    them for a clone new since."""
    if all(clone.url != progress.url for clone in cloning):
        return (*cloning, progress)
    return tuple(progress if clone.url == progress.url else clone for clone in cloning)


def clones_left(
    cloning: Sequence[WorkspaceCloning],
    before: Sequence[WorkspaceEntry],
    after: Sequence[WorkspaceEntry],
) -> tuple[WorkspaceCloning, ...]:
    """The clones of ``cloning`` still under way once the workspaces ``before`` are listed as
    ``after``: those no new workspace is labelled for, by the clone's name, numbered or not, as
    the server names a clone's directory."""
    known = {entry.id for entry in before}
    new = [entry.label for entry in after if entry.id not in known]
    return tuple(clone for clone in cloning if not _listed(clone, new))


def _listed(clone: WorkspaceCloning, labels: Sequence[str]) -> bool:
    """Whether one of the workspaces ``labels`` label may be ``clone``'s, named for it,
    numbered or not."""
    named = re.compile(rf"{re.escape(clone_name(clone.url))}(?:-\d+)?")
    return any(named.fullmatch(label) for label in labels)


def clone_failed(message: str, url: str) -> bool:
    """Whether a ``git_failed`` error's ``message`` says the clone of ``url`` failed."""
    return message.startswith(f"Couldn't clone {url}: ")


def remove_title(entry: WorkspaceEntry) -> str:
    """What removing ``entry``'s workspace asks."""
    return f"Stop serving {entry.label}"


def remove_choices(entry: WorkspaceEntry) -> tuple[str, str, str]:
    """What removing ``entry``'s workspace offers: keeping its sessions, deleting them, naming
    its directory if it's a clone orbit made, as that goes too, or cancelling."""
    purge = PURGE_CHOICE
    if entry.managed:
        purge = f"{PURGE_CHOICE}, and its clone directory {entry.path}"
    return KEEP_CHOICE, purge, CANCEL_CHOICE


def removal(entry: WorkspaceEntry, index: int) -> RemoveWorkspace | None:
    """Removing ``entry``'s workspace as the choice at ``index`` of :func:`remove_choices`
    says: keeping its sessions, deleting them too, or nothing on cancelling."""
    if index == CANCEL_INDEX:
        return None
    return RemoveWorkspace(workspace=entry.id, purge=index == 1)


def added_workspace(workspaces: Sequence[WorkspaceEntry], path: str) -> WorkspaceEntry | None:
    """The workspace among ``workspaces`` that adding ``path`` added: the deepest whose path is
    ``path`` or contains it, as a server takes a path inside a repository for its top level;
    none if it isn't listed yet. A workspace new since then elsewhere, as another frontend
    adds, isn't it. ``..`` and ``.`` in ``path`` are collapsed as the server resolves them, but
    symlinks can't be resolved here."""
    path = posixpath.normpath(path)
    deepest = max(workspaces, key=lambda entry: _depth_in(entry.path, path), default=None)
    if deepest is not None and _depth_in(deepest.path, path):
        return deepest
    return None


def _depth_in(workspace: str | None, path: str) -> int:
    """How many components the ``workspace`` path has if it's ``path`` or contains it,
    compared component by component, so ``/src/repoA/`` and ``/src/repoA/sub`` are in
    ``/src/repoA`` but ``/src/repoAB`` isn't; else 0."""
    if workspace is None:
        return 0
    parts = PurePosixPath(workspace).parts
    return len(parts) if PurePosixPath(path).parts[: len(parts)] == parts else 0


def with_states(
    workspaces: Sequence[WorkspaceEntry], changed: SessionStateChanged
) -> tuple[WorkspaceEntry, ...]:
    """``workspaces``, their sessions as ``changed`` says."""
    return tuple(
        entry.model_copy(
            update={"sessions": tuple(with_state(summary, changed) for summary in entry.sessions)}
        )
        for entry in workspaces
    )
