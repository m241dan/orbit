"""Picking a workspace: the picker shown at startup over a link and on ctrl+w, adding the
repository or a path, removing a workspace, and moving to another workspace's sessions."""

from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from orbit.protocol import (
    BUSY,
    UNKNOWN,
    AddWorkspace,
    AgentRef,
    Attach,
    Catalog,
    CommandRefused,
    Connected,
    Reconnecting,
    RemoveWorkspace,
    SessionRef,
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
from orbit.tui.widgets import ChoiceScreen
from orbit.tui.workspaces import WorkspacesScreen
from tests.server_fakes import URL
from tests.tui_fakes import FakeLink, FakeSession, changes, feed_events, opened

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


def refusal(app: OrbitApp, code: str, message: str) -> CommandRefused:
    return CommandRefused(command_id=last_change(app), code=code, message=message)


def agent(workspace: str, role: str, number: int) -> AgentRef:
    return AgentRef(
        workspace=workspace, session="s", agent_id=f"agent-{number}", role=role, task_id="t"
    )


async def test_the_usage_totals_change_nothing_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        session = session_of(app)
        await feed_events(session, pilot, SERVED)
        totals = UsageTotals(workspaces=(), total=UsageTotal(today=Usage(), lifetime=Usage()))
        await feed_events(session, pilot, totals)
        assert rows(workspaces(app)) == [B_ROW, A_ROW, "Add a path or URL…"]


PROJ_URL = "https://github.com/o/proj.git"


def cloned(
    phase: str, percent: int | None = None, workspace: str | None = None
) -> WorkspaceCloning:
    """The news of the clone of ``PROJ_URL``: at ``phase``, ``percent`` through it, or served as
    ``workspace``."""
    return WorkspaceCloning(
        url=PROJ_URL, name="proj", phase=phase, percent=percent, workspace=workspace
    )


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


async def test_a_local_orbit_never_shows_the_workspace_picker() -> None:
    session = FakeSession()
    app = OrbitApp(session, opening=Opening(repository="/src/repoA"))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SERVED)
        await pilot.press("ctrl+w")
        assert not any(isinstance(s, WorkspacesScreen) for s in app.screen_stack)
        assert session.sent[-1].payload == Attach(workspace="A", session="a1")


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
