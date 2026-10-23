"""The list of workspaces a control plane serves, to pick one from, add one or remove one."""

import posixpath
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    PLANNER,
    REVIEWER,
    SUPER_REVIEWER,
    Activity,
    AddWorkspace,
    AgentRef,
    RemoveWorkspace,
    SessionStateChanged,
    WorkspaceCloning,
    WorkspaceEntry,
    looks_like_url,
    with_state,
)
from orbit.tui.view.common import ListHints, active_session, counted, ordinal
from orbit.tui.view.tasks import coder_wait

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
ROLE_ORDER = (CODER, CONFIGURATOR, REVIEWER, SUPER_REVIEWER, PLANNER)
"""The order the agents summary counts the built-in roles in, before any others."""

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
    """Adding what the user typed, ``text``: by its ``url`` if it's a repository's URL, as the
    server takes one (:func:`looks_like_url`), else by its ``path``."""
    if looks_like_url(text):
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


def row_label(row: WorkspaceRow, agents: Sequence[AgentRef] = ()) -> str:
    """What the picker shows for ``row``, with the ``agents`` running in its workspace."""
    if isinstance(row, AddWorkspace):
        return f"Add {row.path}"
    if isinstance(row, WorkspaceEntry):
        return workspace_label(row, agents)
    if isinstance(row, WorkspaceCloning):
        return clone_label(row)
    return ADD_PATH_LABEL


def workspace_label(entry: WorkspaceEntry, agents: Sequence[AgentRef] = ()) -> str:
    """A workspace's row: its label, its path, its active session's name and state, the agents
    among ``agents`` running in it by role, where its active session waits for a coder, and
    ``read-only`` while another orbit owns it: ``orbit · /src/orbit · today, running · 2 coders
    · waiting: 2nd``."""
    details = [entry.label] if entry.path is None else [entry.label, entry.path]
    details.extend(activity_notes(entry, agents))
    if entry.read_only:
        details.append(READ_ONLY_NOTE)
    return " · ".join(details)


def activity_notes(entry: WorkspaceEntry, agents: Sequence[AgentRef]) -> list[str]:
    """What's going on in ``entry``'s workspace: its active session's name and state, the
    agents among ``agents`` running in it by role, and where its active session waits for a
    coder, each only while there's one."""
    active = active_session(entry.sessions)
    state = None if active is None else f"{active.name}, {active.state}"
    running = agents_summary(roles_in(agents, entry.id))
    wait = None if active is None else wait_note(active.activity)
    return [note for note in (state, running, wait) if note]


def roles_in(agents: Sequence[AgentRef], workspace: str) -> list[str]:
    """The roles of the ``agents`` running in the workspace ``workspace``, one per agent."""
    return [agent.role for agent in agents if agent.workspace == workspace]


def agents_summary(roles: Sequence[str]) -> str:
    """How many agents run in each of ``roles``, one per agent: the built-in roles in
    :data:`ROLE_ORDER`, then others as first named: ``2 coders · 1 reviewer``; empty for
    none."""
    order = {role: index for index, role in enumerate(ROLE_ORDER)}
    named = sorted(dict.fromkeys(roles), key=lambda role: order.get(role, len(order)))
    return " · ".join(counted(roles.count(role), role.replace("-", " ")) for role in named)


def wait_note(activity: Sequence[Activity]) -> str | None:
    """Where a session running ``activity`` waits in line for a coder: ``waiting: 2nd``,
    ``waiting: next`` at 1, and just ``waiting`` without a position; none while it doesn't
    wait."""
    wait = coder_wait(activity)
    if wait is None or wait.position is None:
        return None if wait is None else "waiting"
    place = "next" if wait.position == 1 else ordinal(wait.position)
    return f"waiting: {place}"


def clone_label(cloning: WorkspaceCloning) -> str:
    """A clone's row: the name the server gives it, else its URL, and how far through its phase
    git is, or the phase while git doesn't say: ``Cloning proj… 42%``."""
    progress = cloning.phase if cloning.percent is None else f"{cloning.percent}%"
    return f"Cloning {cloning.name or cloning.url}… {progress}"


def with_clone(
    cloning: Sequence[WorkspaceCloning], progress: WorkspaceCloning
) -> tuple[WorkspaceCloning, ...]:
    """The clones ``cloning``, with ``progress`` in place of the last of its URL's, or after
    them for a clone new since; without its URL's once ``progress`` says it's served, as the
    workspace listed takes its place."""
    if progress.workspace is not None:
        return without_clone(cloning, progress.url)
    if all(clone.url != progress.url for clone in cloning):
        return (*cloning, progress)
    return tuple(progress if clone.url == progress.url else clone for clone in cloning)


def without_clone(cloning: Sequence[WorkspaceCloning], url: str) -> tuple[WorkspaceCloning, ...]:
    """The clones ``cloning`` but that of ``url``."""
    return tuple(clone for clone in cloning if clone.url != url)


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
