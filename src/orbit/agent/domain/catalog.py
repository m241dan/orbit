"""The catalog of what exists: the workspace and its sessions, the agents' roles, the triggers
and the lanes."""

from collections.abc import Iterable

from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    DEFAULT_WORKSPACE,
    PLANNER,
    PLANNER_LANE,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    WORK_LANE,
    AgentEntry,
    Catalog,
    SessionSummary,
    TriggerEntry,
    WorkspaceEntry,
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


def catalog(
    directory: str | None, sessions: tuple[SessionSummary, ...], roles: Iterable[str]
) -> Catalog:
    """The catalog of the repository in the ``directory`` named so, if any, holding
    ``sessions``, with agents in ``roles``."""
    label = directory or NO_WORKSPACE_LABEL
    entry = WorkspaceEntry(id=DEFAULT_WORKSPACE, label=label, sessions=sessions)
    return Catalog(
        workspaces=(entry,),
        agents=tuple(agent_entry(role) for role in roles),
        triggers=TRIGGERS,
        lanes=LANES,
    )
