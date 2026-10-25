"""The catalog of what exists: the workspaces served and their sessions, the agents' roles, the
triggers and the lanes."""

from collections.abc import Iterable, Mapping, Sequence

from orbit.agent.domain.roles import Role, disabled_roles
from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    PLANNER,
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
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

TRIGGER_ROLES = {SUPER_REVIEW: SUPER_REVIEWER, CONFIGURE_CHECKS: CONFIGURATOR}
"""The role of the task each built-in trigger queues."""

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
    managed: bool = False,
    live: Iterable[SessionStateChanged] = (),
) -> WorkspaceEntry:
    """The catalog's entry for the ``workspace``, its id, then the name and path of its
    directory, if it has one, holding ``sessions``, those ``live`` is about in those states,
    ``read_only`` if another orbit owns its repository, ``managed`` if it's a clone orbit
    made."""
    workspace_id, name, path = workspace
    label = name or NO_WORKSPACE_LABEL
    states = {state.session: state for state in live}
    shown = tuple(
        with_state(summary, states[summary.id]) if summary.id in states else summary
        for summary in sessions
    )
    return WorkspaceEntry(
        id=workspace_id,
        label=label,
        path=path,
        read_only=read_only,
        managed=managed,
        sessions=shown,
    )


def trigger_names(roles: Mapping[str, Role]) -> tuple[str, ...]:
    """The names of the built-in triggers but those whose role is disabled among ``roles``."""
    disabled = disabled_roles(roles)
    return tuple(name for name in TRIGGER_NAMES if TRIGGER_ROLES[name] not in disabled)


def catalog(
    workspaces: Sequence[WorkspaceEntry],
    roles: Iterable[str],
    triggers: Iterable[str],
    lanes: Iterable[str],
) -> Catalog:
    """The catalog of the ``workspaces`` served, with agents in ``roles``, the built-in
    ``triggers`` named, in their order, and ``lanes``, each listed once."""
    named = frozenset(triggers)
    return Catalog(
        workspaces=tuple(workspaces),
        agents=tuple(agent_entry(role) for role in dict.fromkeys(roles)),
        triggers=tuple(trigger for trigger in TRIGGERS if trigger.name in named),
        lanes=tuple(dict.fromkeys(lanes)),
    )
