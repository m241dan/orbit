"""The catalog of what exists: the workspaces served and their sessions, the agents' roles, the
triggers and the lanes."""

from collections.abc import Collection, Iterable, Mapping, Sequence

from orbit.agent.domain.roles import Role, disabled_roles, enabled_roles
from orbit.agent.domain.triggers import Trigger, fires_by_hand
from orbit.protocol import (
    BUGS_REVIEWER,
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    DRIFT_REVIEWER,
    EXPERIMENTER,
    FIT_REVIEWER,
    LOGIC_REVIEWER,
    PERFORMANCE_REVIEWER,
    PLANNER,
    REVIEWER,
    SCALING_REVIEWER,
    SECURITY_REVIEWER,
    STYLE_REVIEWER,
    SUPER_REVIEW,
    TESTS_REVIEWER,
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
    EXPERIMENTER: "Experimenter",
    CODER: "Coder",
    REVIEWER: "Reviewer",
    CONFIGURATOR: "Configurator",
    FIT_REVIEWER: "Fit reviewer",
    BUGS_REVIEWER: "Bugs reviewer",
    TESTS_REVIEWER: "Tests reviewer",
    LOGIC_REVIEWER: "Logic reviewer",
    SECURITY_REVIEWER: "Security reviewer",
    PERFORMANCE_REVIEWER: "Performance reviewer",
    STYLE_REVIEWER: "Style reviewer",
    DRIFT_REVIEWER: "Drift reviewer",
    SCALING_REVIEWER: "Scaling reviewer",
}
"""The built-in roles' labels; a custom role is labelled by its name."""

TRIGGERS = (
    TriggerEntry(name=SUPER_REVIEW, label="Super review", manual=True),
    TriggerEntry(name=CONFIGURE_CHECKS, label="Set up checks", manual=True),
)
"""The built-in triggers."""

TRIGGER_NAMES = tuple(trigger.name for trigger in TRIGGERS)
"""The built-in triggers' names."""

_BUILT_IN_LABELS = {trigger.name: trigger.label for trigger in TRIGGERS}
"""The built-in triggers' labels, by name."""

NO_WORKSPACE_LABEL = "default"
"""The workspace's label outside a repository."""


def agent_entry(
    name: str, role: Role, conversational: bool, personal: bool, usage_known: bool = True
) -> AgentEntry:
    """The catalog's entry for the agents of ``role``, named ``name``, ``conversational`` if the
    user can message them, ``personal`` if it's defined whole in the user config, with its lane,
    the defaults of its tasks' settings and its runtime, and whether their usage is known
    (``usage_known``)."""
    return AgentEntry(
        name=name,
        label=ROLE_LABELS.get(name, name),
        conversational=conversational,
        lane=role.lane,
        model=role.model,
        workspace_level=role.workspace_level,
        web=role.web,
        effort=role.effort,
        personal=personal,
        runtime=role.runtime,
        usage_known=usage_known,
    )


def agent_entries(
    roles: Mapping[str, Role],
    conversational: Iterable[str],
    personal: Iterable[str],
    usage_unknown: Iterable[str] = (),
) -> tuple[AgentEntry, ...]:
    """The catalog's entries for the agents of ``roles``, by name, those in ``conversational``
    ones the user can message, those in ``personal`` personal and those in ``usage_unknown``
    ones whose usage is unknown."""
    talking, own, unknown = frozenset(conversational), frozenset(personal), frozenset(usage_unknown)
    return tuple(
        agent_entry(name, role, name in talking, name in own, name not in unknown)
        for name, role in roles.items()
    )


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


def trigger_names(triggers: Mapping[str, Trigger], roles: Mapping[str, Role]) -> tuple[str, ...]:
    """The names of the built-in triggers the user can fire: those among a workspace's
    ``triggers``, by name, that fire by hand with the enabled roles among ``roles``."""
    enabled = enabled_roles(roles)
    return tuple(
        name
        for name in TRIGGER_NAMES
        if name in triggers and fires_by_hand(triggers[name], enabled)
    )


def trigger_entries(
    triggers: Mapping[str, Trigger], roles: Mapping[str, Role]
) -> tuple[TriggerEntry, ...]:
    """The catalog's entries for a workspace's ``triggers``, by name, with its agents' ``roles``:
    each trigger that's enabled and queues a task for a role that isn't disabled, in order, with
    its tab and roles, then each built-in trigger that can be fired there but isn't among them,
    such as one running a named set. An entry is ``manual`` if it's a built-in trigger that its
    trigger lets fire by hand there, and labelled as
    the built-in one is, else as its trigger is, else by its name."""
    fireable = trigger_names(triggers, roles)
    listed = _listed_triggers(triggers, disabled_roles(roles))
    rest = (entry for entry in TRIGGERS if entry.name in fireable and entry.name not in listed)
    return (
        *(_trigger_entry(name, trigger, name in fireable) for name, trigger in listed.items()),
        *rest,
    )


def _listed_triggers(
    triggers: Mapping[str, Trigger], disabled: Collection[str]
) -> dict[str, Trigger]:
    """The ``triggers`` the catalog lists, by name, in order: those enabled that queue a task for
    a role not among the ``disabled``."""
    return {
        name: trigger
        for name, trigger in triggers.items()
        if trigger.enabled and any(role not in disabled for role in trigger.roles)
    }


def _trigger_entry(name: str, trigger: Trigger, manual: bool) -> TriggerEntry:
    """The catalog's entry for ``trigger``, named ``name``, ``manual`` if the user can fire
    it."""
    label = _BUILT_IN_LABELS.get(name) or trigger.label or name
    return TriggerEntry(name=name, label=label, manual=manual, tab=trigger.tab, roles=trigger.roles)


def catalog(
    workspaces: Sequence[WorkspaceEntry],
    roles: Iterable[tuple[str, Role, bool, bool]],
    triggers: Iterable[TriggerEntry],
    lanes: Iterable[str],
    conversational: Iterable[str],
) -> Catalog:
    """The catalog of the ``workspaces`` served, with agents in ``roles``, each given with its
    name, whether it's personal and whether its agents' usage is known, and named once, as the
    first of that name is, those among them in ``conversational`` ones the user can message, the
    ``triggers``, each named once, as the first of that name is, and ``lanes``, each listed
    once."""
    named: dict[str, TriggerEntry] = {}
    for trigger in triggers:
        named.setdefault(trigger.name, trigger)
    talking = frozenset(conversational)
    first: dict[str, tuple[Role, bool, bool]] = {}
    for name, role, personal, usage_known in roles:
        first.setdefault(name, (role, personal, usage_known))
    return Catalog(
        workspaces=tuple(workspaces),
        agents=tuple(
            agent_entry(name, role, name in talking, personal, usage_known)
            for name, (role, personal, usage_known) in first.items()
        ),
        triggers=tuple(named.values()),
        lanes=tuple(dict.fromkeys(lanes)),
    )
