"""Picking a workspace: the picker shown at startup over a link and on ctrl+w, adding the
repository or a path, removing a workspace, and moving to another workspace's sessions."""

from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from orbit.protocol import (
    BUSY,
    CONTROL_PLANE_STREAM,
    GIT_FAILED,
    UNKNOWN,
    AddWorkspace,
    AgentRef,
    AgentsChanged,
    Attach,
    Catalog,
    CommandAccepted,
    CommandRefused,
    Connected,
    DirectoryListing,
    ErrorOccurred,
    NewSession,
    Reconnecting,
    RemoveWorkspace,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Usage,
    UsageTotal,
    UsageTotals,
    Watch,
    WorkspaceCloning,
    WorkspaceEntry,
)
from orbit.tui import Opening, OrbitApp
from orbit.tui.sessions import SessionsScreen
from orbit.tui.view.workspaces import STARTUP_HINT
from orbit.tui.widgets import ChoiceScreen
from orbit.tui.workspaces import WorkspacesScreen
from tests.server_fakes import URL
from tests.tui_fakes import FakeLink, FakeSession, changes, feed_events, opened, watches

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


def last_change(app: OrbitApp) -> str:
    """The id of the last command sent but for watching streams, which closing the picker
    sends."""
    envelopes = session_of(app).envelopes
    return next(e.command_id for e in reversed(envelopes) if not isinstance(e.payload, Watch))


async def typing(app: OrbitApp, pilot: Pilot[None]) -> None:
    """Open the picker's path box, which ``Add a path or URL…`` no longer opens, until the
    explorer's field takes typed paths and URLs."""
    workspaces(app).picker.type_path()
    await pilot.pause()


async def accept_last(app: OrbitApp, pilot: Pilot[None]) -> None:
    await session_of(app).queue.put(CommandAccepted(command_id=last_change(app)))


def refusal(app: OrbitApp, code: str, message: str) -> CommandRefused:
    return CommandRefused(command_id=last_change(app), code=code, message=message)


WATCHING = Watch(streams=(CONTROL_PLANE_STREAM,))


def agent(workspace: str, role: str, number: int) -> AgentRef:
    return AgentRef(
        workspace=workspace, session="s", agent_id=f"agent-{number}", role=role, task_id="t"
    )


async def test_the_picker_watches_the_control_plane_while_it_is_open() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        assert watches(session) == [WATCHING]
        await pilot.press("enter", "enter")
        await pilot.pause()
        assert watches(session) == [WATCHING, Watch(streams=())]
        await pilot.press("ctrl+w")
        await pilot.pause()
        assert watches(session) == [WATCHING, Watch(streams=()), WATCHING]
        await pilot.press("escape")
        await pilot.pause()
        assert watches(session) == [WATCHING, Watch(streams=()), WATCHING, Watch(streams=())]


async def test_each_workspace_row_shows_its_running_agents_by_role() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        running = (agent("A", "coder", 1), agent("A", "reviewer", 2), agent("A", "coder", 3))
        await feed_events(session, pilot, AgentsChanged(agents=running))
        a_row = f"{A_ROW} · 2 coders · 1 reviewer"
        assert rows(workspaces(app)) == [B_ROW, a_row, "Add a path or URL…"]
        await feed_events(session, pilot, AgentsChanged(agents=()))
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


async def test_a_picker_opened_again_shows_no_agents_until_they_are_sent_again() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await feed_events(session, pilot, AgentsChanged(agents=(agent("B", "planner", 1),)))
        await pilot.press("enter", "enter", "ctrl+w")
        await pilot.pause()
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


async def test_agents_changing_while_the_picker_is_closed_show_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("enter", "enter")
        await feed_events(session, pilot, AgentsChanged(agents=(agent("B", "coder", 1),)))
        await pilot.press("ctrl+w")
        await pilot.pause()
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


async def test_the_usage_totals_change_nothing_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        totals = UsageTotals(workspaces=(), total=UsageTotal(today=Usage(), lifetime=Usage()))
        await feed_events(session, pilot, totals)
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


async def test_a_directory_listing_changes_nothing_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await feed_events(session, pilot, DirectoryListing(path="/srv", entries=(), total=0))
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


PROJ_URL = "https://github.com/o/proj.git"
PROJ = WorkspaceEntry(id="P", label="proj", path="/srv/clones/proj", managed=True, sessions=())


def cloned(
    phase: str, percent: int | None = None, workspace: str | None = None
) -> WorkspaceCloning:
    """The news of the clone of ``PROJ_URL``: at ``phase``, ``percent`` through it, or served as
    ``workspace``."""
    return WorkspaceCloning(
        url=PROJ_URL, name="proj", phase=phase, percent=percent, workspace=workspace
    )


async def test_a_clones_progress_shows_in_its_row_until_it_is_served() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await feed_events(session, pilot, cloned("Counting objects"))
        assert rows(workspaces(app))[2] == "Cloning proj… Counting objects"
        await feed_events(session, pilot, cloned("Receiving objects", percent=42))
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Cloning proj… 42%", "Add a path or URL…"]
        await feed_events(session, pilot, catalog(B, A, PROJ))
        await feed_events(session, pilot, cloned("Done", percent=100, workspace="P"))
        assert rows(workspaces(app))[2:] == ["proj · /srv/clones/proj", "Add a path or URL…"]


async def test_a_clone_under_way_shows_in_a_picker_opened_later() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("enter", "enter")
        await feed_events(session, pilot, cloned("Resolving"))
        await pilot.press("ctrl+w")
        assert rows(workspaces(app))[2] == "Cloning proj… Resolving"


async def test_a_url_typed_is_sent_as_one_closing_the_field_once_accepted() -> None:
    url = PROJ_URL
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await typing(app, pilot)
        await pilot.press(*url, "enter")
        assert changes(session) == [AddWorkspace(url=url)]
        await accept_last(app, pilot)
        await pilot.pause()
        assert not workspaces(app).query_one(Input).display


async def test_a_clone_failing_drops_its_row_and_shows_gits_message_in_the_fields_hint() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await typing(app, pilot)
        await pilot.press(*PROJ_URL, "enter")
        await accept_last(app, pilot)
        await feed_events(session, pilot, cloned("Resolving"))
        failed = f"Couldn't clone {PROJ_URL}: Repository not found."
        await feed_events(
            session, pilot, ErrorOccurred(code=GIT_FAILED, message=failed, url=PROJ_URL)
        )
        screen = workspaces(app)
        assert rows(screen) == [B_ROW, A_ROW, "Add a path or URL…"]
        field = screen.query_one(Input)
        assert field.display and field.value == PROJ_URL
        assert field.border_subtitle == failed


async def test_a_clone_failing_with_the_picker_closed_drops_its_row() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("enter", "enter")
        await feed_events(session, pilot, cloned("Resolving"))
        failed = f"Couldn't clone {PROJ_URL}: Repository not found."
        await feed_events(
            session, pilot, ErrorOccurred(code=GIT_FAILED, message=failed, url=PROJ_URL)
        )
        await pilot.press("ctrl+w")
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


async def test_reconnecting_drops_the_clones_from_before() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await feed_events(session, pilot, cloned("Resolving"))
        link = app._link
        assert isinstance(link, FakeLink)
        await link.move(pilot, Reconnecting(URL, app.clock()))
        assert rows(workspaces(app))[2] == "Cloning proj… Resolving"
        await link.move(pilot, Connected(URL))
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


async def test_picking_a_workspace_opens_its_session_picker_whose_pick_attaches() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter")
        screen = sessions(app)
        assert screen.startup and screen.workspace == "A"
        assert rows(screen)[:2] == ["  Resume today · active", "  earlier"]
        await pilot.press("j", "enter")
        assert changes(session_of(app))[0] == Attach(workspace="A", session="a0")


async def test_resuming_attaches_to_the_active_session() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter")
        assert changes(session_of(app)) == [Attach(workspace="A", session="a1")]


async def test_a_repository_not_served_is_offered_first_and_added_then_its_sessions_shown() -> None:
    app = connected("/src/repoC")
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        assert rows(workspaces(app)) == ["Add /src/repoC", B_ROW, A_ROW, "Add a path or URL…"]
        assert highlighted(workspaces(app)) == 0
        await pilot.press("enter")
        assert changes(session) == [AddWorkspace(path="/src/repoC")]
        await accept_last(app, pilot)
        await feed_events(session, pilot, catalog(B, A, C))
        screen = sessions(app)
        assert screen.workspace == "C"
        await pilot.press("enter")
        assert changes(session)[-1] == Attach(workspace="C", session="c1")


async def test_adding_a_path_sends_it_and_a_refusal_shows_in_the_fields_hint() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await typing(app, pilot)
        path = workspaces(app).query_one(Input)
        assert path.display and app.focused is path
        await pilot.press(*"/nowhere", "enter")
        assert changes(session) == [AddWorkspace(path="/nowhere")]
        await feed_events(session, pilot, refusal(app, UNKNOWN, "There's no /nowhere."))
        assert path.border_subtitle == "There's no /nowhere."
        assert workspaces(app).note is None


async def test_x_keeps_a_workspaces_sessions_by_default_and_a_busy_refusal_shows() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        await pilot.press("k", "x")
        choice = app.screen
        assert isinstance(choice, ChoiceScreen)
        assert choice.title_text == "Stop serving repoB"
        assert choice.choices == ("Keep its sessions", "Delete its sessions too", "Cancel")
        await pilot.press("enter")
        assert changes(session) == [RemoveWorkspace(workspace="B", purge=False)]
        busy = "Can't remove /src/repoB: it's running task 3."
        await feed_events(session, pilot, refusal(app, BUSY, busy))
        assert workspaces(app).note == busy


async def test_a_state_change_shows_in_the_workspaces_row() -> None:
    app = connected()
    async with app.run_test() as pilot:
        running = SessionStateChanged(session="b1", active=True, state="running", viewers=1)
        await feed_events(session_of(app), pilot, SERVED, running)
        assert rows(workspaces(app))[0] == "repoB · /src/repoB · flags, running"


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
        assert changes(session_of(app))[-1] == Attach(workspace="B", session="b1")
        await pilot.press("ctrl+p")
        assert sessions(app).workspace == "B"


def mark(app: OrbitApp) -> str | None:
    """The viewing mark by the prompt; none while it's hidden."""
    line = app.query_one("#viewing", Static)
    return str(line.render()) if line.display else None


async def viewing_earlier_a(app: OrbitApp, pilot: Pilot[None]) -> None:
    """Join repoA at startup and view its earlier session read-only, then open repoB's
    sessions."""
    await feed_events(session_of(app), pilot, SERVED)
    await pilot.press("enter", "enter")
    read_only = {"active": False, "open_session": SessionRef(id="a1", name="today")}
    await feed_events(session_of(app), pilot, opened(EARLIER_A).model_copy(update=read_only))
    assert mark(app) is not None
    await pilot.press("ctrl+w", "k", "enter")


async def test_moving_to_another_workspace_clears_the_viewing_mark_right_away() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await viewing_earlier_a(app, pilot)
        await pilot.press("enter")
        assert mark(app) is None


async def test_moving_to_another_workspace_failing_to_attach_stays_viewing_where_it_was() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await viewing_earlier_a(app, pilot)
        shown = mark(app)
        session_of(app).send_error = RuntimeError("the backend is gone")
        await pilot.press("enter")
        assert mark(app) == shown
        await pilot.press("escape", "ctrl+p")  # Past the picker shown again, saying why.
        assert sessions(app).workspace == "A"


async def test_a_join_at_startup_failing_to_attach_joins_nothing_and_sends_no_opening() -> None:
    app = connected(opening=Opening(NewSession(name="auth"), repository="/src/repoA"))
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        session_of(app).send_error = RuntimeError("the backend is gone")
        await pilot.press("enter")
        assert changes(session_of(app)) == [Attach(workspace="A", session="a1")]
        assert (app._navigation.place.workspace, app._navigation.place.attached) == (None, None)


async def test_a_new_session_in_another_workspace_attaches_there_first() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter", "j", "enter", "enter")
        assert changes(session_of(app))[-2:] == [
            Attach(workspace="B", session="b1"),
            NewSession(),
        ]


async def test_escape_from_another_workspaces_sessions_stays_on_the_workspace_viewed() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter", "escape", "ctrl+p")
        assert sessions(app).workspace == "A"
        assert changes(session_of(app)) == [Attach(workspace="A", session="a1")]


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
        assert rows(screen) == ["Add /src/repoA", B_ROW, "Add a path or URL…"]


async def test_the_workspace_viewed_removed_opens_a_picker_whose_escape_quits() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter")
        await feed_events(session_of(app), pilot, catalog(B))
        screen = workspaces(app)
        assert screen.startup
        assert screen.query_one(Vertical).border_subtitle == STARTUP_HINT
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_code == 0


async def test_opening_with_a_command_attaches_to_the_workspace_picked_then_sends_it() -> None:
    app = connected(opening=Opening(NewSession(name="auth"), repository="/src/repoA"))
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter")
        assert changes(session_of(app)) == [
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
        assert changes(session_of(app)) == []


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


async def test_removing_the_workspace_viewed_from_the_picker_keeps_one_picker() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "x", "enter")
        await feed_events(session_of(app), pilot, catalog(B))
        assert [type(s) for s in app.screen_stack[1:]] == [WorkspacesScreen]
        assert changes(session_of(app))[-1] == RemoveWorkspace(workspace="A")


async def test_removing_the_workspace_viewed_from_the_picker_leaves_one_escape_quits() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "x", "enter")
        await feed_events(session_of(app), pilot, catalog(B))
        screen = workspaces(app)
        assert screen.startup
        assert screen.query_one(Vertical).border_subtitle == STARTUP_HINT
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_code == 0


async def test_picking_after_removing_the_workspace_viewed_opens_sessions_escape_quits() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "x", "enter")
        await feed_events(session_of(app), pilot, catalog(B))
        workspaces(app).query_one(OptionList).highlighted = 1
        await pilot.press("enter")
        screen = sessions(app)
        assert screen.startup and screen.workspace == "B"
        await pilot.press("escape")
        await pilot.pause()
    assert app.return_code == 0


def opening_auth() -> OrbitApp:
    """An app linked to a control plane, opening on repoA with a new session named auth."""
    return connected(opening=Opening(NewSession(name="auth"), repository="/src/repoA"))


async def pick_b_after_the_loss(app: OrbitApp, pilot: Pilot[None]) -> None:
    """Pick repoB in the picker the loss of repoA leaves shown."""
    await feed_events(session_of(app), pilot, catalog(B))
    workspaces(app).query_one(OptionList).highlighted = 1
    await pilot.press("enter")
    await pilot.pause()


def joined_b_once(app: OrbitApp) -> None:
    """Check the app attached to repoB's active session, having sent auth only once."""
    sent = changes(session_of(app))
    assert sent[-1] == Attach(workspace="B", session="b1")
    assert sent.count(NewSession(name="auth")) == 1
    assert not any(isinstance(s, SessionsScreen) for s in app.screen_stack)


async def test_picking_after_removing_the_workspace_viewed_attaches_its_active_session() -> None:
    app = opening_auth()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "ctrl+w", "x", "enter")
        await pick_b_after_the_loss(app, pilot)
        joined_b_once(app)


async def test_picking_in_the_picker_a_loss_opens_attaches_its_active_session() -> None:
    app = opening_auth()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "ctrl+w", "x", "enter", "escape")
        assert not any(isinstance(s, WorkspacesScreen) for s in app.screen_stack)
        await pick_b_after_the_loss(app, pilot)
        joined_b_once(app)


async def test_picking_from_the_picker_without_a_loss_opens_sessions_escape_goes_back() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter", "ctrl+w", "k", "enter")
        assert not sessions(app).startup
        await pilot.press("escape")
        assert len(app.screen_stack) == 1
        assert changes(session_of(app)) == [Attach(workspace="A", session="a1")]


async def test_a_pick_takes_the_workspace_picked_though_the_list_changed_before_it_is_taken() -> (
    None
):
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        screen = workspaces(app)
        screen.query_one(OptionList).action_select()
        screen.picker.show((A, B))  # Puts B where A was.
        await pilot.pause()
        assert sessions(app).workspace == "A"


async def test_a_pick_whose_workspace_left_the_list_before_it_is_taken_does_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        screen = workspaces(app)
        screen.query_one(OptionList).action_select()
        screen.picker.show((B,))  # Puts adding a path where A was.
        await pilot.pause()
        assert app.screen is screen
        assert changes(session_of(app)) == []
