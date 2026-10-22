"""Browsing the server's directories in the workspace picker's explorer: listing, moving,
adding and opening workspaces, and refusals."""

from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from orbit.protocol import (
    INVALID,
    AddWorkspace,
    Catalog,
    CommandAccepted,
    CommandRefused,
    DirectoryEntry,
    DirectoryListing,
    ListDirectory,
    SessionSummary,
    WorkspaceEntry,
)
from orbit.tui import Opening, OrbitApp
from orbit.tui.explorer import ExplorerScreen
from orbit.tui.sessions import SessionsScreen
from orbit.tui.workspaces import WorkspacesScreen
from tests.tui_fakes import FakeLink, FakeSession, changes, feed_events

A = WorkspaceEntry(
    id="A", label="a", path="/srv/a", sessions=(SessionSummary(id="a1", name="t", active=True),)
)
B = WorkspaceEntry(id="B", label="repoB", path="/src/repoB", sessions=())
SERVED = Catalog(workspaces=(B,), agents=(), triggers=(), lanes=())
REPO_A = DirectoryEntry(name="a", is_repository=True, hidden=False)
SERVED_C = DirectoryEntry(name="c", is_repository=True, workspace="B", hidden=False)
PLAIN = DirectoryEntry(name="docs", is_repository=False, hidden=False)
LINK = DirectoryEntry(name="link", is_repository=False, hidden=False, target="/opt/x")
DOT = DirectoryEntry(name=".cache", is_repository=False, hidden=True)
SRV = DirectoryListing(
    path="/srv", parent="/", entries=(REPO_A, SERVED_C, DOT, PLAIN, LINK), total=5
)
SRV_A = DirectoryListing(path="/srv/a", parent="/srv", entries=(PLAIN,), total=1)
OPT_X = DirectoryListing(path="/opt/x", parent="/opt", entries=(), total=0)
SRV_ROWS = ["⎇ a", "⎇ c (served)", "docs", "link → /opt/x"]


def connected() -> OrbitApp:
    session = FakeSession()
    session.attached = True
    return OrbitApp(session, FakeLink(), Opening(pick=True, repository=None))


def session_of(app: OrbitApp) -> FakeSession:
    session = app._session
    assert isinstance(session, FakeSession)
    return session


def explorer(app: OrbitApp) -> ExplorerScreen:
    screen = app.screen
    assert isinstance(screen, ExplorerScreen)
    return screen


def rows(screen: ExplorerScreen) -> list[str]:
    options = screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def more(screen: ExplorerScreen) -> str | None:
    shown = screen.query_one("#explorer-more", Static)
    return str(shown.render()) if shown.display else None


async def explore(app: OrbitApp, pilot: Pilot[None], *listings: DirectoryListing) -> None:
    """Open the explorer from the picker at startup, and have the server send ``listings``."""
    await feed_events(session_of(app), pilot, SERVED)
    await pilot.press("j", "enter")
    await feed_events(session_of(app), pilot, *listings)


async def answer(app: OrbitApp, pilot: Pilot[None], refused: str | None = None) -> None:
    """Accept the last command sent, or refuse it as ``invalid`` saying ``refused``."""
    command_id = session_of(app).envelopes[-1].command_id
    if refused is None:
        await feed_events(session_of(app), pilot, CommandAccepted(command_id=command_id))
    else:
        refusal = CommandRefused(command_id=command_id, code=INVALID, message=refused)
        await feed_events(session_of(app), pilot, refusal)


async def test_opening_lists_the_starting_directory_with_marks_hiding_hidden_ones() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        assert changes(session_of(app)) == [ListDirectory(path=None)]
        screen = explorer(app)
        assert screen.query_one(Vertical).border_title == "/srv"
        assert rows(screen) == SRV_ROWS
        field = screen.query_one(Input)
        assert field.display and app.focused is screen.query_one(OptionList)
        assert more(screen) is None


async def test_dot_shows_hidden_directories_and_hides_them_again() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("j", "j", "full_stop")
        screen = explorer(app)
        assert rows(screen) == ["⎇ a", "⎇ c (served)", ".cache", "docs", "link → /opt/x"]
        assert screen.query_one(OptionList).highlighted == 3
        await pilot.press("k", "full_stop")
        assert rows(screen) == SRV_ROWS
        assert screen.query_one(OptionList).highlighted == 2
        assert changes(session_of(app)) == [ListDirectory(path=None)]


async def test_showing_hidden_ones_of_nothing_shown_highlights_the_first() -> None:
    app = connected()
    async with app.run_test() as pilot:
        hidden_only = DirectoryListing(path="/srv", entries=(DOT,), total=1)
        await explore(app, pilot, hidden_only)
        await pilot.press("full_stop")
        assert explorer(app).query_one(OptionList).highlighted == 0


async def test_l_lists_the_directory_highlighted_ignoring_a_late_listing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        assert changes(session_of(app))[-1] == ListDirectory(path="/srv/a")
        await feed_events(session_of(app), pilot, SRV)
        screen = explorer(app)
        assert rows(screen) == SRV_ROWS
        await feed_events(session_of(app), pilot, SRV_A)
        assert screen.query_one(Vertical).border_title == "/srv/a"
        assert rows(screen) == ["docs"]
        await answer(app, pilot)
        assert rows(screen) == ["docs"]


async def test_h_lists_the_parent_and_nothing_at_the_top() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV_A)
        await pilot.press("h")
        assert changes(session_of(app))[-1] == ListDirectory(path="/srv")
        top = DirectoryListing(path="/srv", entries=(), total=0)
        await feed_events(session_of(app), pilot, top)
        await pilot.press("h", "l", "a")
        assert changes(session_of(app))[-1] == ListDirectory(path="/srv")


async def test_keys_before_a_listing_send_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot)
        await pilot.press("h", "l", "a", "full_stop")
        assert changes(session_of(app)) == [ListDirectory(path=None)]
        assert explorer(app).query_one(Vertical).border_title == ""


async def test_enter_lists_a_directory_and_a_symlinks_listing_shows_once_accepted() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("j", "j", "j", "enter")
        assert changes(session_of(app))[-1] == ListDirectory(path="/srv/link")
        await feed_events(session_of(app), pilot, OPT_X)
        screen = explorer(app)
        assert rows(screen) == SRV_ROWS
        await answer(app, pilot)
        assert screen.query_one(Vertical).border_title == "/opt/x"
        assert rows(screen) == []
        await feed_events(session_of(app), pilot, SRV)
        assert rows(screen) == []


async def test_enter_on_a_served_directory_opens_its_workspace() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("j", "enter")
        screen = app.screen
        assert isinstance(screen, SessionsScreen) and screen.workspace == "B"
        assert not any(isinstance(s, WorkspacesScreen) for s in app.screen_stack)


async def test_a_adds_the_directory_and_opens_its_sessions_once_listed() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("a")
        assert changes(session_of(app))[-1] == AddWorkspace(path="/srv/a")
        await answer(app, pilot)
        await pilot.pause()
        assert isinstance(app.screen, WorkspacesScreen)
        catalog = Catalog(workspaces=(B, A), agents=(), triggers=(), lanes=())
        await feed_events(session_of(app), pilot, catalog)
        screen = app.screen
        assert isinstance(screen, SessionsScreen) and screen.workspace == "A"


async def test_a_workspace_listed_before_its_add_is_accepted_opens_on_acceptance() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("a")
        catalog = Catalog(workspaces=(B, A), agents=(), triggers=(), lanes=())
        await feed_events(session_of(app), pilot, catalog)
        await answer(app, pilot)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SessionsScreen) and screen.workspace == "A"


async def test_a_refused_listing_shows_why_under_the_listing_it_had() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        await answer(app, pilot, "Permission denied: /srv/a")
        screen = explorer(app)
        assert screen.note == "Permission denied: /srv/a"
        assert rows(screen) == SRV_ROWS
        await feed_events(session_of(app), pilot, SRV_A)
        assert rows(screen) == SRV_ROWS
        await pilot.press("l")
        assert screen.note is None


async def test_a_refused_add_shows_why_and_keeps_a_listing_asked_for() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l", "a")
        await answer(app, pilot, "/srv/docs is outside the browsable root /srv/a.")
        screen = explorer(app)
        assert screen.note == "/srv/docs is outside the browsable root /srv/a."
        await feed_events(session_of(app), pilot, SRV_A)
        assert rows(screen) == ["docs"]


async def test_a_truncated_listing_says_how_many_are_left_out() -> None:
    app = connected()
    async with app.run_test() as pilot:
        truncated = DirectoryListing(path="/srv", entries=(PLAIN,), truncated=True, total=2400)
        await explore(app, pilot, truncated)
        assert more(explorer(app)) == "… 2399 more not shown: type the path in the field"
        await pilot.press("l")
        docs = DirectoryListing(path="/srv/docs", entries=(), total=0)
        await feed_events(session_of(app), pilot, docs)
        assert more(explorer(app)) is None


async def test_escape_returns_to_the_list_picker() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("escape")
        assert isinstance(app.screen, WorkspacesScreen)
        await feed_events(session_of(app), pilot, SRV_A)
        assert isinstance(app.screen, WorkspacesScreen)


async def test_an_answer_once_the_explorer_closed_changes_nothing() -> None:
    for refused in (None, "Permission denied: /srv/a"):
        app = connected()
        async with app.run_test() as pilot:
            await explore(app, pilot, SRV)
            await pilot.press("a", "escape")
            await answer(app, pilot, refused)
            assert isinstance(app.screen, WorkspacesScreen)
            assert app.screen.note is None


async def test_a_listing_with_the_picker_closed_shows_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await feed_events(session_of(app), pilot, SERVED)
        await pilot.press("enter", "enter")
        await feed_events(session_of(app), pilot, SRV)
        assert [type(s) for s in app.screen_stack[1:]] == [SessionsScreen]
