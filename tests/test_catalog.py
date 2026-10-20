"""The catalog of what exists, sent after Welcome and whenever the sessions change."""

import asyncio
from collections.abc import Mapping

from orbit.agent import Agents, WorkspaceHost
from orbit.agent.domain.catalog import catalog
from orbit.agent.domain.roles import Role
from orbit.protocol import (
    CONFIGURE_CHECKS,
    DEFAULT_WORKSPACE,
    PLANNER_LANE,
    SUPER_REVIEW,
    WORK_LANE,
    AgentEntry,
    Catalog,
    Command,
    CommandEnvelope,
    Event,
    NewSession,
    RenameSession,
    SessionSummary,
    Snapshot,
    TriggerEntry,
    event_adapter,
)
from tests.agent_fakes import (
    ROLES,
    FakeHost,
    FakeStore,
    ScriptedLauncher,
    Ticks,
    frontend,
    greet,
)

SECURITY = Role(instructions="Look for security holes.")


def session_in(
    workspace: str | None, roles: Mapping[str, Role] = ROLES, store: FakeStore | None = None
) -> WorkspaceHost:
    """A session in the repository at ``workspace``, with agents in ``roles``, saved in
    ``store`` if given."""
    host = FakeHost()
    agents = Agents(host, ScriptedLauncher(host), lambda: "token", roles=roles)
    return WorkspaceHost(
        agents=agents, workspace=workspace, store=store, clock=Ticks(), new_id=lambda: "s1"
    )


async def published(session: WorkspaceHost, count: int) -> list[Event]:
    """The next ``count`` events ``session`` publishes."""
    stream = frontend(session).events()
    return [(await asyncio.wait_for(anext(stream), 1)).payload for _ in range(count)]


async def after(session: WorkspaceHost, command: Command, count: int) -> list[Event]:
    """The first ``count`` events ``session`` publishes for ``command``, once greeted and
    past its Welcome, Catalog, Snapshot, workspace note and the change counting its viewer."""
    await greet(session)
    await published(session, 5)
    await frontend(session).send(CommandEnvelope(command_id="c", payload=command))
    return await published(session, count)


def entry_of(catalog: Event) -> tuple[str, ...]:
    """The names of the sessions in ``catalog``'s one workspace."""
    assert isinstance(catalog, Catalog)
    [workspace] = catalog.workspaces
    return tuple(session.name for session in workspace.sessions)


async def test_the_catalog_after_welcome_lists_the_workspace_agents_triggers_and_lanes() -> None:
    session = session_in("/x/proj")
    await greet(session)
    _, catalog, snapshot = await published(session, 3)
    await session.close()

    assert isinstance(catalog, Catalog)
    assert isinstance(snapshot, Snapshot)
    [workspace] = catalog.workspaces
    assert (workspace.id, workspace.label) == (DEFAULT_WORKSPACE, "proj")
    viewed = snapshot.session.model_copy(update={"active": True, "viewers": 1})
    assert workspace.sessions == (viewed,)
    assert [(a.label, a.conversational) for a in catalog.agents] == [
        ("Planner", True),
        ("Coder", False),
        ("Reviewer", False),
        ("Super reviewer", False),
        ("Configurator", False),
    ]
    assert catalog.triggers == (
        TriggerEntry(name=SUPER_REVIEW, label="Super review", manual=True),
        TriggerEntry(name=CONFIGURE_CHECKS, label="Set up checks", manual=True),
    )
    assert catalog.lanes == (WORK_LANE, PLANNER_LANE)


async def test_a_project_role_is_listed_by_its_name_and_not_conversational() -> None:
    session = session_in("/x/proj", {**ROLES, "security-reviewer": SECURITY})
    await greet(session)
    _, catalog = await published(session, 2)
    await session.close()

    assert isinstance(catalog, Catalog)
    assert catalog.agents[-1] == AgentEntry(
        name="security-reviewer", label="security-reviewer", conversational=False
    )


async def test_a_session_without_agents_catalogs_none_in_the_default_workspace() -> None:
    session = WorkspaceHost()
    await greet(session)
    _, catalog = await published(session, 2)
    await session.close()

    assert isinstance(catalog, Catalog)
    assert catalog.agents == ()
    assert catalog.workspaces[0].label == "default"


async def test_a_new_session_is_in_a_catalog_before_its_snapshot() -> None:
    session = session_in("/x/proj", store=FakeStore())
    catalog, snapshot = await after(session, NewSession(name="other"), 2)
    await session.close()

    assert entry_of(catalog) == ("other",)
    assert isinstance(snapshot, Snapshot)


async def test_renaming_the_open_session_sends_a_catalog_with_the_new_name() -> None:
    session = session_in("/x/proj", store=FakeStore())
    [catalog] = await after(session, RenameSession(session_id="s1", name="auth"), 1)
    await session.close()

    assert entry_of(catalog) == ("auth",)


async def test_a_refused_rename_sends_no_catalog() -> None:
    session = session_in("/x/proj", store=FakeStore())
    events = await after(session, RenameSession(session_id="s1", name=" "), 2)
    await session.close()

    assert [type(e).__name__ for e in events] == ["SessionsListed", "CommandRefused"]


def test_a_catalog_round_trips() -> None:
    sessions = (SessionSummary(id="s1", name="auth", current=True),)
    made = catalog("proj", sessions, ("planner", "security-reviewer"))
    assert event_adapter.validate_json(made.model_dump_json()) == made


async def test_the_catalog_belongs_to_no_session() -> None:
    session = WorkspaceHost()
    await greet(session)
    stream = frontend(session).events()
    await anext(stream)
    envelope = await asyncio.wait_for(anext(stream), 1)
    await session.close()

    assert isinstance(envelope.payload, Catalog)
    assert envelope.session is None
