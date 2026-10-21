"""The catalog of what exists: the workspaces served and their sessions, the agents' roles, the
triggers and the lanes."""

from collections.abc import Iterable, Sequence

from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    PLANNER,
    PLANNER_LANE,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    WORK_LANE,
    AgentEntry,
    Catalog,
    SessionStateChanged,
    SessionSummary,
    TriggerEntry,
    WorkspaceEntry,
    with_state,
)

ROLE_LABELS = {
    PLANNER: "Planner",
    CODER: "Coder",
    REVIEWER: "Reviewer",
    SUPER_REVIEWER: "Super reviewer",
    CONFIGURATOR: "Configurator",
}
"""The built-in roles' labels; a custom role is labelled by its name."""

CONVERSATIONAL_ROLES = (PLANNER,)
"""The roles the user can message."""

TRIGGERS = (
    TriggerEntry(name=SUPER_REVIEW, label="Super review", manual=True),
    TriggerEntry(name=CONFIGURE_CHECKS, label="Set up checks", manual=True),
)
"""The built-in triggers."""

TRIGGER_NAMES = tuple(trigger.name for trigger in TRIGGERS)
"""The built-in triggers' names."""

LANES = (WORK_LANE, PLANNER_LANE)
"""The lanes work runs in."""

NO_WORKSPACE_LABEL = "default"
"""The workspace's label outside a repository."""


def agent_entry(role: str) -> AgentEntry:
    """The catalog's entry for the agents of ``role``."""
    label = ROLE_LABELS.get(role, role)
    return AgentEntry(name=role, label=label, conversational=role in CONVERSATIONAL_ROLES)


def workspace_entry(
    workspace: tuple[str, str | None, str | None],
    sessions: tuple[SessionSummary, ...],
    *,
    read_only: bool = False,
    live: Iterable[SessionStateChanged] = (),
) -> WorkspaceEntry:
    """The catalog's entry for the ``workspace``, its id, then the name and path of its
    directory, if it has one, holding ``sessions``, those ``live`` is about in those states,
    ``read_only`` if another orbit owns its repository."""
    workspace_id, name, path = workspace
    label = name or NO_WORKSPACE_LABEL
    states = {state.session: state for state in live}
    shown = tuple(
        with_state(summary, states[summary.id]) if summary.id in states else summary
        for summary in sessions
    )
    return WorkspaceEntry(
        id=workspace_id, label=label, path=path, read_only=read_only, sessions=shown
    )


def catalog(workspaces: Sequence[WorkspaceEntry], roles: Iterable[str]) -> Catalog:
    """The catalog of the ``workspaces`` served, with agents in ``roles``, each listed once."""
    return Catalog(
        workspaces=tuple(workspaces),
        agents=tuple(agent_entry(role) for role in dict.fromkeys(roles)),
        triggers=TRIGGERS,
        lanes=LANES,
    )
