"""Apps that pick a session at startup, and the sessions they list."""

from datetime import UTC, datetime

from orbit.protocol import (
    ActivateSession,
    AgentEntry,
    Attach,
    Catalog,
    SessionStateChanged,
    SessionSummary,
    WorkspaceEntry,
)
from orbit.tui import Opening, OrbitApp
from tests.tui_fakes import FakeLink, FakeSession

ACTIVE = datetime(2026, 10, 16, 18, 2, tzinfo=UTC)


OLD = SessionSummary(id="old", name="auth rework", last_active=ACTIVE)


CURRENT = SessionSummary(
    id="now", name="today", last_active=ACTIVE, current=True, active=True, viewers=1
)


LOCKED = SessionSummary(id="busy", name="elsewhere", locked=True)


BROKEN = SessionSummary(id="bad", name="bad.json", unreadable="not JSON")


RUNNING = SessionStateChanged(session="now", active=True, state="running", viewers=1)


SWITCH_TO_OLD = [Attach(workspace="w", session="old"), ActivateSession(session="old")]


def catalog(*sessions: SessionSummary) -> Catalog:
    workspace = WorkspaceEntry(id="w", label="project", sessions=sessions)
    return Catalog(workspaces=(workspace,), agents=(), triggers=(), lanes=())


CATALOG = catalog(CURRENT, OLD, LOCKED, BROKEN)


def with_agents(*agents: AgentEntry) -> Catalog:
    """:data:`CATALOG`, its workspace configuring the roles of ``agents``, which it lists."""
    workspaces = tuple(w.model_copy(update={"agents": agents}) for w in CATALOG.workspaces)
    return CATALOG.model_copy(update={"agents": agents, "workspaces": workspaces})


def in_process() -> tuple[OrbitApp, FakeSession]:
    """An app picking a session at startup over a backend in this process."""
    session = FakeSession()
    return OrbitApp(session, opening=Opening(pick=True)), session


def joined() -> tuple[OrbitApp, FakeSession]:
    """An app picking a session at startup over a control plane it's linked to, once the user
    picks the workspace, which it attaches to only then."""
    session = FakeSession()
    session.attached = True  # The user's pick attaches, so its Attach is one of the commands.
    return OrbitApp(session, FakeLink(), Opening(pick=True)), session


def started() -> tuple[OrbitApp, FakeSession]:
    """An app that picks no session at startup."""
    session = FakeSession()
    return OrbitApp(session), session
