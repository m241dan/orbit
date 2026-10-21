"""The list of workspaces a control plane serves, to pick one from, add one or remove one."""

from collections.abc import Collection, Sequence
from pathlib import PurePosixPath
from typing import Literal

from orbit.protocol import AddWorkspace, SessionStateChanged, WorkspaceEntry, with_state

WORKSPACES_TITLE = "Workspaces"
ADD_PATH: Literal["add_path"] = "add_path"
ADD_PATH_LABEL = "Add a path…"
PATH_PLACEHOLDER = "Absolute path on the server's host"
READ_ONLY_NOTE = "read-only"
REMOVE_TITLE = "Remove workspace"
REMOVE_YES = "remove"
LIST_HINT = "j/k move · enter open · x remove · esc close"
STARTUP_HINT = "j/k move · enter open · x remove · esc quit"
PATH_HINT = "enter add · esc back"

WorkspaceRow = WorkspaceEntry | AddWorkspace | Literal["add_path"]
"""A row of the picker: a workspace served, adding the repository the frontend runs in, or
adding a path typed in."""


def workspace_rows(
    workspaces: Sequence[WorkspaceEntry], repository: str | None
) -> tuple[WorkspaceRow, ...]:
    """The rows the picker lists: adding the ``repository`` the frontend runs in first, if
    there's one and none of ``workspaces`` is at it, then ``workspaces``, then adding a path."""
    served = any(entry.path == repository for entry in workspaces)
    first = [] if repository is None or served else [AddWorkspace(path=repository)]
    return (*first, *workspaces, ADD_PATH)


def preselected(rows: Sequence[WorkspaceRow], repository: str | None) -> int:
    """The index of the row of the workspace at the ``repository`` the frontend runs in among
    ``rows``, else the first's."""
    at_repository = (
        index
        for index, row in enumerate(rows)
        if isinstance(row, WorkspaceEntry) and row.path == repository
    )
    return next(at_repository, 0)


def row_key(row: WorkspaceRow) -> str:
    """What tells ``row`` apart from the picker's other rows, as its workspace's sessions
    change."""
    if isinstance(row, AddWorkspace):
        return f"add {row.path}"
    return row.id if isinstance(row, WorkspaceEntry) else row


def row_label(row: WorkspaceRow) -> str:
    """What the picker shows for ``row``."""
    if isinstance(row, AddWorkspace):
        return f"Add {row.path}"
    if isinstance(row, WorkspaceEntry):
        return workspace_label(row)
    return ADD_PATH_LABEL


def workspace_label(entry: WorkspaceEntry) -> str:
    """A workspace's row: its label, its path, its active session's name and state, and
    ``read-only`` while another orbit owns it: ``orbit · /src/orbit · today, idle``."""
    details = [entry.label] if entry.path is None else [entry.label, entry.path]
    active = next((summary for summary in entry.sessions if summary.active), None)
    if active is not None:
        details.append(f"{active.name}, {active.state}")
    if entry.read_only:
        details.append(READ_ONLY_NOTE)
    return " · ".join(details)


def remove_message(entry: WorkspaceEntry) -> str:
    """What removing ``entry``'s workspace asks the user to confirm."""
    return f'Stop serving "{entry.label}"? Its sessions are kept, for when it\'s added again.'


def added_workspace(
    workspaces: Sequence[WorkspaceEntry], path: str, before: Collection[str]
) -> WorkspaceEntry | None:
    """The workspace among ``workspaces`` that adding ``path`` added: the deepest whose path is
    ``path`` or contains it, as a server takes a path inside a repository for its top level,
    else the first whose id isn't among those listed ``before``; none if neither is listed
    yet."""
    deepest = max(workspaces, key=lambda entry: _depth_in(entry.path, path), default=None)
    if deepest is not None and _depth_in(deepest.path, path):
        return deepest
    return next((entry for entry in workspaces if entry.id not in before), None)


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


def workspaces_hint(*, startup: bool, adding: bool) -> str:
    """The keys the workspace picker shows: the path box's while ``adding``, or else the
    list's, where escape quits at ``startup``."""
    if adding:
        return PATH_HINT
    return STARTUP_HINT if startup else LIST_HINT
