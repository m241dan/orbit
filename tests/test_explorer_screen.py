"""Browsing the server's directories in the workspace picker's explorer: listing, moving,
adding and opening workspaces, typing and completing paths in its field, and refusals."""

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
    WorkspaceCloning,
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
ORBIT = DirectoryEntry(name="orbit", is_repository=True, hidden=False)
ORCHARD = DirectoryEntry(name="orchard", is_repository=False, hidden=False)
SRC = DirectoryListing(path="/src", parent="/", entries=(ORBIT, ORCHARD, DOT), total=3)
PROJECTS = DirectoryEntry(name="projects", is_repository=False, hidden=False)
WORK = DirectoryListing(path="/work", parent="/", entries=(PROJECTS,), total=1)
ROOT = DirectoryListing(
    path="/", entries=(DirectoryEntry(name="srv", is_repository=False, hidden=False),), total=1
)
REAL = DirectoryListing(path="/opt/real", parent="/opt", entries=(PROJECTS,), total=1)
REPO = WorkspaceEntry(id="R", label="repo", path="/srv/repo", sessions=())


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


async def test_a_on_a_symlink_adds_its_target_and_opens_its_sessions_once_listed() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("j", "j", "j", "a")
        assert changes(session_of(app))[-1] == AddWorkspace(path="/opt/x")
        await answer(app, pilot)
        await pilot.pause()
        x = WorkspaceEntry(id="X", label="x", path="/opt/x", sessions=())
        catalog = Catalog(workspaces=(B, x), agents=(), triggers=(), lanes=())
        await feed_events(session_of(app), pilot, catalog)
        screen = app.screen
        assert isinstance(screen, SessionsScreen) and screen.workspace == "X"


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


def field_of(app: OrbitApp) -> Input:
    return explorer(app).query_one("#explorer-path", Input)


def candidates(app: OrbitApp) -> str | None:
    shown = explorer(app).query_one("#explorer-candidates", Static)
    return str(shown.render()) if shown.display else None


async def typed(app: OrbitApp, pilot: Pilot[None], text: str) -> None:
    """Focus the explorer's field with ``/`` and have ``text`` typed in it."""
    await pilot.press("slash")
    field = field_of(app)
    field.value = text
    field.cursor_position = len(text)


async def test_slash_focuses_the_field_where_keys_type_and_escape_returns() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("slash")
        field = field_of(app)
        assert app.focused is field
        await pilot.press("slash", "a", "h", "l", "full_stop")
        assert field.value == "/ahl."
        await pilot.press("escape")
        assert isinstance(app.screen, ExplorerScreen)
        assert explorer(app).query_one(OptionList).has_focus
        assert changes(session_of(app)) == [ListDirectory(path=None)]


async def test_tab_completes_one_match_from_the_listing_with_a_slash() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/src/orb")
        await pilot.press("tab")
        field = field_of(app)
        assert field.value == "/src/orbit/"
        assert app.focused is field
        assert candidates(app) is None
        assert changes(session_of(app)) == [ListDirectory(path=None)]


async def test_tab_completes_several_matches_prefix_and_shows_them() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/src/or")
        await pilot.press("tab")
        assert field_of(app).value == "/src/or"
        assert candidates(app) == "orbit  orchard"
        field_of(app).value = "/src/orc"
        await pilot.press("tab")
        assert field_of(app).value == "/src/orchard/"
        assert candidates(app) is None


async def test_tab_with_no_match_changes_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/src/x")
        await pilot.press("tab")
        assert field_of(app).value == "/src/x"
        assert candidates(app) is None


async def test_tab_completes_hidden_directories_only_while_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/src/.c")
        await pilot.press("tab")
        assert field_of(app).value == "/src/.c"
        await pilot.press("escape", "full_stop")
        await pilot.press("slash", "tab")
        assert field_of(app).value == "/src/.cache/"


async def test_tab_elsewhere_lists_the_parent_for_completion_alone() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/work/pro")
        await pilot.press("tab")
        assert changes(session_of(app))[-1] == ListDirectory(path="/work")
        assert field_of(app).value == "/work/pro"
        await feed_events(session_of(app), pilot, WORK)
        assert field_of(app).value == "/work/pro"
        await answer(app, pilot)
        assert field_of(app).value == "/work/projects/"
        screen = explorer(app)
        assert screen.query_one(Vertical).border_title == "/src"
        assert rows(screen) == ["⎇ orbit", "orchard"]
        await feed_events(session_of(app), pilot, WORK)
        assert rows(screen) == ["⎇ orbit", "orchard"]
        assert field_of(app).value == "/work/projects/"


async def test_a_completion_listing_once_typed_on_completes_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/work/pro")
        await pilot.press("tab")
        field_of(app).value = "/srv/a"
        await feed_events(session_of(app), pilot, WORK)
        await answer(app, pilot)
        assert field_of(app).value == "/srv/a"


async def test_tab_without_a_path_does_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "src")
        await pilot.press("tab")
        assert field_of(app).value == "src"
        assert changes(session_of(app)) == [ListDirectory(path=None)]


async def test_a_completion_listing_of_the_directory_asked_for_is_also_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        await typed(app, pilot, "/srv/a/d")
        await pilot.press("tab")
        await feed_events(session_of(app), pilot, SRV_A)
        assert rows(explorer(app)) == ["docs"]
        await answer(app, pilot)
        assert field_of(app).value == "/srv/a/docs/"
        assert rows(explorer(app)) == ["docs"]


async def test_a_completion_listing_at_its_resolved_path_completes_the_path_typed() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/link/pro")
        await pilot.press("tab")
        assert changes(session_of(app))[-1] == ListDirectory(path="/link")
        await feed_events(session_of(app), pilot, REAL)
        await answer(app, pilot)
        assert field_of(app).value == "/link/projects/"
        screen = explorer(app)
        assert screen.query_one(Vertical).border_title == "/src"
        assert rows(screen) == ["⎇ orbit", "orchard"]


async def test_a_completion_listing_while_a_listing_is_awaited_is_never_shown() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        listed = len(session_of(app).envelopes) - 1
        await typed(app, pilot, "/link/pro")
        await pilot.press("tab")
        await feed_events(session_of(app), pilot, REAL)
        await answer_to(app, pilot, listed, None)
        screen = explorer(app)
        assert rows(screen) == SRV_ROWS
        await answer(app, pilot)
        assert field_of(app).value == "/link/projects/"
        assert rows(screen) == SRV_ROWS
        await pilot.press("escape", "l", "j", "j", "j", "enter")
        await feed_events(session_of(app), pilot, OPT_X)
        await answer(app, pilot)
        assert screen.query_one(Vertical).border_title == "/opt/x"


async def test_the_listing_asked_for_while_completing_elsewhere_is_shown_alone() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        await typed(app, pilot, "/link/d")
        await pilot.press("tab")
        await feed_events(session_of(app), pilot, SRV_A)
        assert rows(explorer(app)) == ["docs"]
        await answer(app, pilot)
        assert field_of(app).value == "/link/d"


async def test_an_earlier_completions_outcome_once_another_is_asked_for_completes_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/work/pro")
        await pilot.press("tab")
        earliest = len(session_of(app).envelopes) - 1
        await pilot.press("tab", "tab")
        await answer_to(app, pilot, earliest, None)
        assert field_of(app).value == "/work/pro"
        await answer_to(app, pilot, earliest + 1, "Permission denied: /work")
        await feed_events(session_of(app), pilot, WORK)
        await answer(app, pilot)
        assert field_of(app).value == "/work/projects/"


async def test_a_completion_answered_once_the_explorer_closed_changes_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/work/pro")
        await pilot.press("tab", "escape", "escape")
        await answer(app, pilot)
        assert isinstance(app.screen, WorkspacesScreen)


async def test_a_refused_completion_listing_shows_in_the_fields_hint() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/root/x")
        await pilot.press("tab")
        await answer(app, pilot, "Permission denied: /root")
        assert field_of(app).border_subtitle == "Permission denied: /root"
        assert explorer(app).note is None


async def test_a_refused_completion_leaves_nothing_to_complete_from() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRC)
        await typed(app, pilot, "/work/pro")
        await pilot.press("tab")
        await answer(app, pilot, "Permission denied: /work")
        await pilot.press("escape", "l")
        await feed_events(session_of(app), pilot, WORK)
        await answer(app, pilot)
        assert field_of(app).value == "/work/pro"
        assert explorer(app).query_one(Vertical).border_title == "/work"


async def test_enter_in_the_field_adds_the_path_and_opens_its_sessions_once_listed() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await typed(app, pilot, " /srv/repo ")
        await pilot.press("enter")
        assert changes(session_of(app))[-1] == AddWorkspace(path="/srv/repo")
        await answer(app, pilot)
        await pilot.pause()
        assert isinstance(app.screen, WorkspacesScreen)
        catalog = Catalog(workspaces=(B, REPO), agents=(), triggers=(), lanes=())
        await feed_events(session_of(app), pilot, catalog)
        screen = app.screen
        assert isinstance(screen, SessionsScreen) and screen.workspace == "R"


async def test_enter_in_the_field_adds_a_url_and_returns_to_the_list_picker() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await typed(app, pilot, "https://github.com/o/r")
        await pilot.press("enter")
        assert changes(session_of(app))[-1] == AddWorkspace(url="https://github.com/o/r")
        await answer(app, pilot)
        await pilot.pause()
        assert isinstance(app.screen, WorkspacesScreen)
        cloning = WorkspaceCloning(
            url="https://github.com/o/r", name="r", phase="Receiving objects"
        )
        await feed_events(session_of(app), pilot, cloning)
        options = app.screen.query_one(OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert "Cloning r… Receiving objects" in labels


async def test_a_refused_add_from_the_field_shows_in_its_hint_until_the_next() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await typed(app, pilot, "/etc")
        await pilot.press("enter")
        await answer(app, pilot, "/etc is outside the browsable root /srv.")
        field = field_of(app)
        assert field.border_subtitle == "/etc is outside the browsable root /srv."
        assert explorer(app).note is None and app.focused is field
        await pilot.press("enter")
        assert field.border_subtitle == ""


async def test_enter_in_an_empty_field_adds_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await typed(app, pilot, "  ")
        await pilot.press("enter")
        assert changes(session_of(app)) == [ListDirectory(path=None)]
        assert isinstance(app.screen, ExplorerScreen)


async def test_a_field_add_answered_once_the_explorer_closed_changes_nothing() -> None:
    for refused in (None, "Permission denied: /srv/a"):
        app = connected()
        async with app.run_test() as pilot:
            await explore(app, pilot, SRV)
            await typed(app, pilot, "/srv/repo")
            await pilot.press("enter", "escape", "escape")
            await answer(app, pilot, refused)
            assert isinstance(app.screen, WorkspacesScreen)
            assert app.screen.note is None


async def test_a_pick_takes_the_directory_picked_though_the_list_changed_before_it_is_taken() -> (
    None
):
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("j", "j")
        screen = explorer(app)
        screen.query_one(OptionList).action_select()
        screen.action_hidden()  # Puts .cache where docs was.
        await pilot.pause()
        assert changes(session_of(app)) == [
            ListDirectory(path=None),
            ListDirectory(path="/srv/docs"),
        ]


async def test_a_pick_whose_directory_left_the_list_before_it_is_taken_does_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("j", "j")
        screen = explorer(app)
        screen.query_one(OptionList).action_select()
        screen._show(SRV_A)
        await pilot.pause()
        assert explorer(app) is screen
        assert changes(session_of(app)) == [ListDirectory(path=None)]


async def answer_to(app: OrbitApp, pilot: Pilot[None], index: int, refused: str | None) -> None:
    """Accept the command sent at ``index``, or refuse it as ``invalid`` saying ``refused``."""
    command_id = session_of(app).envelopes[index].command_id
    if refused is None:
        await feed_events(session_of(app), pilot, CommandAccepted(command_id=command_id))
    else:
        refusal = CommandRefused(command_id=command_id, code=INVALID, message=refused)
        await feed_events(session_of(app), pilot, refusal)


async def test_an_earlier_listings_outcome_once_another_is_asked_for_changes_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        earlier = len(session_of(app).envelopes) - 1
        await pilot.press("h")
        assert changes(session_of(app))[-1] == ListDirectory(path="/")
        await feed_events(session_of(app), pilot, SRV_A)
        await answer_to(app, pilot, earlier, None)
        screen = explorer(app)
        assert rows(screen) == SRV_ROWS
        await feed_events(session_of(app), pilot, ROOT)
        await answer(app, pilot)
        assert screen.query_one(Vertical).border_title == "/"
        assert rows(screen) == ["srv"]


async def test_an_earlier_listings_refusal_once_another_is_asked_for_shows_nothing() -> None:
    app = connected()
    async with app.run_test() as pilot:
        await explore(app, pilot, SRV)
        await pilot.press("l")
        earlier = len(session_of(app).envelopes) - 1
        await pilot.press("h")
        await answer_to(app, pilot, earlier, "Permission denied: /srv/a")
        screen = explorer(app)
        assert screen.note is None
        await feed_events(session_of(app), pilot, ROOT)
        assert screen.query_one(Vertical).border_title == "/"
        assert rows(screen) == ["srv"]
