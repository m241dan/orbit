"""The CatalogState keeping the one copy of the latest catalog that both pickers read."""

from textual.app import App

from orbit.protocol import (
    Catalog,
    SessionStateChanged,
    SessionSummary,
    WorkspaceEntry,
)
from orbit.tui.sessions import SessionsHost
from orbit.tui.view.catalog import CatalogState
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.place import Place
from orbit.tui.view.session_picker import SessionChange, SessionChoice
from orbit.tui.view.workspace_list import Served, WorkspaceList
from orbit.tui.workspaces import WorkspacesHost

NOW = SessionSummary(id="now", name="today", current=True, active=True, viewers=1)
OLD = SessionSummary(id="old", name="auth rework")
FAR = SessionSummary(id="far", name="elsewhere", active=True)
RUNNING = SessionStateChanged(session="now", active=True, state="running", viewers=1)
TWO = Catalog(
    workspaces=(
        WorkspaceEntry(id="w1", label="one", sessions=(NOW, OLD)),
        WorkspaceEntry(id="w2", label="two", sessions=(FAR, NOW)),
    ),
    agents=(),
    triggers=(),
    lanes=(),
)


def test_it_lists_nothing_before_a_catalog() -> None:
    state = CatalogState()
    assert (state.workspaces, state.sessions("w1")) == ((), ())


def test_a_catalog_lists_its_workspaces_and_each_ones_sessions() -> None:
    state = CatalogState()
    state.follow(TWO)
    assert state.workspaces == TWO.workspaces
    assert state.sessions("w2") == (FAR, NOW)
    assert state.sessions("gone") == ()
    assert state.sessions(None) == ()


def test_a_state_change_applies_to_its_session_in_every_workspace_alone() -> None:
    state = CatalogState()
    state.follow(TWO)
    state.follow_state(RUNNING)
    assert [summary.state for summary in state.sessions("w1")] == ["running", OLD.state]
    assert [summary.state for summary in state.sessions("w2")] == [FAR.state, "running"]
    assert state.sessions("w1")[1] == OLD
    assert state.sessions("w2")[0] == FAR


def test_a_later_catalog_replaces_the_states_noted() -> None:
    state = CatalogState()
    state.follow(TWO)
    state.follow_state(RUNNING)
    state.follow(TWO)
    assert state.workspaces == TWO.workspaces


async def _send(command: SessionChoice | SessionChange, awaited: Awaited) -> bool:
    return True


async def _change(command: object, awaited: object) -> None:
    """Send nothing."""


async def _chosen(workspace: str, startup: bool) -> None:
    """Pick nothing."""


def test_both_hosts_report_the_state_the_one_holder_notes() -> None:
    app = App[None]()
    state = CatalogState()
    sessions = Place(SessionsHost(app.push_screen, _send), _send, state)
    workspaces = WorkspaceList(
        WorkspacesHost(app.push_screen, _change), _change, _chosen, Served(state)
    )
    state.follow(TWO)
    sessions.follow()
    workspaces.follow()
    state.follow_state(RUNNING)
    sessions.follow_state()
    workspaces.follow()
    assert sessions.attached_session is not None
    assert sessions.attached_session.state == "running"
    assert workspaces.workspaces[0].sessions[0].state == "running"
    assert workspaces.workspaces is state.workspaces
    assert sessions.sessions == state.sessions("w1")
