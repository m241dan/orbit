"""Picking a workspace: the picker shown at startup over a link and on ctrl+w, adding the
repository or a path, removing a workspace, and moving to another workspace's sessions."""

from textual.pilot import Pilot
from textual.widgets import Input, OptionList

from orbit.protocol import (
    BUSY,
    UNKNOWN,
    AddWorkspace,
    Attach,
    Catalog,
    CommandAccepted,
    CommandRefused,
    NewSession,
    RemoveWorkspace,
    SessionStateChanged,
    SessionSummary,
    WorkspaceEntry,
)
from orbit.tui import Opening, OrbitApp
from orbit.tui.sessions import SessionsScreen
from orbit.tui.widgets import ConfirmScreen
from orbit.tui.workspaces import WorkspacesScreen
from tests.tui_fakes import FakeLink, FakeSession, feed_events

TODAY_A = SessionSummary(id="a1", name="today", active=True)
EARLIER_A = SessionSummary(id="a0", name="earlier")
TODAY_B = SessionSummary(id="b1", name="flags", active=True)
TODAY_C = SessionSummary(id="c1", name="new", active=True)
A = WorkspaceEntry(id="A", label="repoA", path="/src/repoA", sessions=(TODAY_A, EARLIER_A))
B = WorkspaceEntry(id="B", label="repoB", path="/src/repoB", sessions=(TODAY_B,))
C = WorkspaceEntry(id="C", label="repoC", path="/src/repoC", sessions=(TODAY_C,))
A_ROW = "repoA · /src/repoA · today, idle"
B_ROW = "repoB · /src/repoB · flags, idle"


def catalog(*workspaces: WorkspaceEntry) -> Catalog:
    return Catalog(workspaces=workspaces, agents=(), triggers=(), lanes=())


SERVED = catalog(B, A)


def connected(repository: str | None = "/src/repoA", opening: Opening | None = None) -> OrbitApp:
    """An app linked to a control plane, run in ``repository``, picking a session at startup
    unless ``opening`` says otherwise; its FakeSession records every Attach."""
    session = FakeSession()
    session.attached = True
    return OrbitApp(session, FakeLink(), opening or Opening(pick=True, repository=repository))


def session_of(app: OrbitApp) -> FakeSession:
    session = app._session
    assert isinstance(session, FakeSession)
    return session


def workspaces(app: OrbitApp) -> WorkspacesScreen:
    screen = app.screen
    assert isinstance(screen, WorkspacesScreen)
    return screen


def sessions(app: OrbitApp) -> SessionsScreen:
    screen = app.screen
    assert isinstance(screen, SessionsScreen)
    return screen


def rows(screen: WorkspacesScreen | SessionsScreen) -> list[str]:
    options = screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def highlighted(screen: WorkspacesScreen) -> int | None:
    return screen.query_one(OptionList).highlighted


async def accept_last(app: OrbitApp, pilot: Pilot[None]) -> None:
    session = session_of(app)
    await session.queue.put(CommandAccepted(command_id=session.envelopes[-1].command_id))


def refusal(app: OrbitApp, code: str, message: str) -> CommandRefused:
    return CommandRefused(
        command_id=session_of(app).envelopes[-1].command_id, code=code, message=message
    )


async def test_connected_the_picker_preselects_the_workspace_at_the_repository() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        screen = workspaces(app)
        assert screen.startup
        assert rows(screen) == [B_ROW, A_ROW, "Add a path…"]
        assert highlighted(screen) == 1
        assert session_of(app).commands == []


async def test_picking_a_workspace_opens_its_session_picker_whose_pick_attaches() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter")
        screen = sessions(app)
        assert screen.startup and screen.workspace == "A"
        assert rows(screen)[:2] == ["  Resume today · active", "  earlier"]
        await pilot.press("j", "enter")
        assert session_of(app).commands[0] == Attach(workspace="A", session="a0")


async def test_resuming_attaches_to_the_active_session() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter")
        assert session_of(app).commands == [Attach(workspace="A", session="a1")]


async def test_without_a_repository_served_the_first_workspace_is_preselected() -> None:
    app = connected(None)
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path…"]
        assert highlighted(workspaces(app)) == 0


async def test_a_repository_not_served_is_offered_first_and_added_then_its_sessions_shown() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        assert rows(workspaces(app)) == ["Add /src/repoC", B_ROW, A_ROW, "Add a path…"]
        assert highlighted(workspaces(app)) == 0
        await pilot.press("enter")
        assert session.commands == [AddWorkspace(path="/src/repoC")]
        await accept_last(app, pilot)
        await feed_events(session, pilot, catalog(B, A, C))
        screen = sessions(app)
        assert screen.workspace == "C"
        await pilot.press("enter")
        assert session.commands[-1] == Attach(workspace="C", session="c1")


async def test_a_workspace_added_and_listed_before_its_acceptance_is_picked_on_it() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("enter")
        await feed_events(session, pilot, catalog(B, A, C))
        assert isinstance(app.screen, WorkspacesScreen)
        assert rows(workspaces(app))[-2] == "repoC · /src/repoC · new, idle"
        await accept_last(app, pilot)
        await pilot.pause()
        assert sessions(app).workspace == "C"


async def test_adding_a_path_sends_it_and_a_refusal_shows_in_the_fields_hint() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("j", "enter")
        path = workspaces(app).query_one(Input)
        assert path.display and app.focused is path
        await pilot.press(*"/nowhere", "enter")
        assert session.commands == [AddWorkspace(path="/nowhere")]
        await feed_events(session, pilot, refusal(app, UNKNOWN, "There's no /nowhere."))
        assert path.border_subtitle == "There's no /nowhere."
        assert workspaces(app).note is None


async def test_a_path_added_under_a_workspace_picks_the_one_it_adds() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("j", "enter", *"/src/repoC/sub", "enter")
        await accept_last(app, pilot)
        await feed_events(session, pilot, catalog(B, A, C))
        assert sessions(app).workspace == "C"


async def test_an_empty_path_sends_nothing_and_escape_goes_back_to_the_list() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("j", "enter", "space", "enter")
        assert session_of(app).commands == []
        await pilot.press("escape")
        screen = workspaces(app)
        assert not screen.query_one(Input).display
        assert app.focused is screen.query_one(OptionList)
        assert app.return_code is None


async def test_an_add_accepted_but_never_listed_keeps_the_picker_open() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("j", "enter", *"/src/repoA/sub", "enter")
        await accept_last(app, pilot)
        await pilot.pause()
        screen = workspaces(app)
        assert not screen.query_one(Input).display
        assert rows(screen) == [B_ROW, A_ROW, "Add a path…"]


async def test_x_removes_a_workspace_once_confirmed_and_a_busy_refusal_shows() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("k", "x")
        confirm = app.screen
        assert isinstance(confirm, ConfirmScreen)
        assert (
            confirm.message
            == 'Stop serving "repoB"? Its sessions are kept, for when it\'s added again.'
        )
        await pilot.press("y")
        assert session.commands == [RemoveWorkspace(workspace="B")]
        busy = "Can't remove /src/repoB: it's running task 3."
        await feed_events(session, pilot, refusal(app, BUSY, busy))
        assert workspaces(app).note == busy


async def test_x_unconfirmed_removes_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("x", "n")
        assert session_of(app).commands == []
        assert isinstance(app.screen, WorkspacesScreen)


async def test_x_on_a_row_adding_a_workspace_does_nothing() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("x")
        assert isinstance(app.screen, WorkspacesScreen)


async def test_a_refused_add_of_the_repository_shows_under_the_list() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter")
        await feed_events(session_of(app), pilot, refusal(app, UNKNOWN, "gone"))
        assert workspaces(app).note == "gone"


async def test_a_removal_listed_while_shown_keeps_the_highlight_on_its_row() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await feed_events(session_of(app), pilot, catalog(A))
        screen = workspaces(app)
        assert rows(screen) == [A_ROW, "Add a path…"]
        assert highlighted(screen) == 0
        await feed_events(session_of(app), pilot, catalog(C, A))
        assert highlighted(screen) == 1


async def test_a_gone_row_highlighted_moves_to_where_it_was() -> None:
    app = connected(None)
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("j")
        await feed_events(session_of(app), pilot, catalog(B))
        assert highlighted(workspaces(app)) == 1


async def test_a_state_change_shows_in_the_workspaces_row() -> None:
    app = connected()
    async with app.run_test() as pilot:
        running = SessionStateChanged(session="b1", active=True, state="running", viewers=1)
        await feed_events(session_of(app), pilot, SERVED, running)
        assert rows(workspaces(app))[0] == "repoB · /src/repoB · flags, running"


async def test_escape_at_startup_quits() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_code == 0


async def test_ctrl_w_reopens_the_picker_and_escape_closes_it() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w")
        screen = workspaces(app)
        assert not screen.startup
        await pilot.press("escape")
        assert not isinstance(app.screen, WorkspacesScreen)
        assert app.return_code is None


async def test_picking_the_workspace_viewed_opens_its_usual_session_picker() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "enter")
        screen = sessions(app)
        assert not screen.startup and screen.joined
        assert rows(screen)[0] == "● today · active"


async def test_moving_to_another_workspace_resumes_its_active_session_first() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter")
        screen = sessions(app)
        assert screen.workspace == "B" and not screen.joined and not screen.startup
        assert rows(screen) == ["  Resume flags · active", "New session"]
        await pilot.press("enter")
        assert session_of(app).commands[-1] == Attach(workspace="B", session="b1")
        await pilot.press("ctrl+p")
        assert sessions(app).workspace == "B"


async def test_a_new_session_in_another_workspace_attaches_there_first() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter", "j", "enter", "enter")
        assert session_of(app).commands[-2:] == [
            Attach(workspace="B", session="b1"),
            NewSession(),
        ]


async def test_escape_from_another_workspaces_sessions_stays_on_the_workspace_viewed() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter", "escape", "ctrl+p")
        assert sessions(app).workspace == "A"
        assert session_of(app).commands == [Attach(workspace="A", session="a1")]


async def test_another_workspaces_sessions_follow_the_catalog_while_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter")
        made = SessionSummary(id="b2", name="made")
        await feed_events(
            session_of(app), pilot, catalog(B.model_copy(update={"sessions": (TODAY_B, made)}), A)
        )
        assert rows(sessions(app)) == ["  Resume flags · active", "  made", "New session"]


async def test_the_workspace_viewed_removed_opens_the_picker() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter")
        await feed_events(session_of(app), pilot, catalog(B))
        screen = workspaces(app)
        assert not screen.startup
        assert rows(screen) == ["Add /src/repoA", B_ROW, "Add a path…"]


async def test_opening_with_a_command_attaches_to_the_workspace_picked_then_sends_it() -> None:
    app = connected(opening=Opening(NewSession(name="auth"), repository="/src/repoA"))
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter")
        assert session_of(app).commands == [
            Attach(workspace="A", session="a1"),
            NewSession(name="auth"),
        ]
        assert not isinstance(app.screen, SessionsScreen)


async def test_opening_with_a_command_on_a_workspace_without_an_active_session_sends_none() -> None:
    app = connected(opening=Opening(NewSession(name="auth"), repository="/src/repoA"))
    async with app.run_test() as pilot:
        idle = A.model_copy(update={"sessions": (EARLIER_A,)})
        await feed_events(session_of(app), pilot, catalog(idle))
        await pilot.press("enter")
        assert session_of(app).commands == []


async def test_a_local_orbit_never_shows_the_workspace_picker() -> None:
    session = FakeSession()
    app = OrbitApp(session, opening=Opening(repository="/src/repoA"))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SERVED)
        await pilot.press("ctrl+w")
        assert not any(isinstance(s, WorkspacesScreen) for s in app.screen_stack)
        assert session.sent[-1].payload == Attach(workspace="A", session="a1")


async def joined_in_repo_c(app: OrbitApp, pilot: Pilot[None]) -> None:
    """Join repoB from the startup picker, then reopen the picker, offering to add repoC."""
    await feed_events(session_of(app), pilot, SERVED)
    await pilot.press("j", "enter", "enter", "ctrl+w")


async def test_an_add_accepted_once_the_picker_closed_opens_nothing() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        await joined_in_repo_c(app, pilot)
        await pilot.press("enter", "escape")
        await accept_last(app, pilot)
        await feed_events(session_of(app), pilot, catalog(B, A, C))
        assert len(app.screen_stack) == 1


async def test_an_add_refused_once_the_picker_closed_shows_nowhere_else() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        await joined_in_repo_c(app, pilot)
        await pilot.press("enter", "escape")
        await feed_events(session_of(app), pilot, refusal(app, UNKNOWN, "gone"))
        assert len(app.screen_stack) == 1


async def test_an_add_listed_while_confirming_a_removal_is_picked_after() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter")
        await accept_last(app, pilot)
        await pilot.press("j", "x")
        await feed_events(session_of(app), pilot, catalog(B, A, C))
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await feed_events(session_of(app), pilot, catalog(B, A, C))
        assert sessions(app).workspace == "C"


async def test_removing_the_workspace_viewed_from_the_picker_keeps_one_picker() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "x", "y")
        await feed_events(session_of(app), pilot, catalog(B))
        assert [type(s) for s in app.screen_stack[1:]] == [WorkspacesScreen]
        assert session_of(app).commands[-1] == RemoveWorkspace(workspace="A")
