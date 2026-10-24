"""The SessionsHost keeping the sessions listed, and showing them in the picker."""

from textual.app import App
from textual.widgets import OptionList

from orbit.protocol import (
    ActivateSession,
    Activity,
    Attach,
    Catalog,
    NewSession,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
    WorkspaceEntry,
)
from orbit.tui.catalog import CatalogState
from orbit.tui.sessions import (
    Placing,
    SessionChange,
    SessionChoice,
    SessionsHost,
    SessionsScreen,
)
from orbit.tui.view.outcomes import Awaited
from orbit.tui.view.sessions import chosen_workspace
from orbit.tui.widgets import ChoiceScreen, ConfirmScreen
from tests.tui_fakes import opened

OLD = SessionSummary(id="old", name="auth rework")
NOW = SessionSummary(id="now", name="today", current=True, active=True, viewers=1)
RUNNING = SessionStateChanged(session="now", active=True, state="running", viewers=1)


def catalog(*sessions: SessionSummary) -> Catalog:
    workspace = WorkspaceEntry(id="w", label="project", sessions=sessions)
    return Catalog(workspaces=(workspace,), agents=(), triggers=(), lanes=())


class Host(App[None]):
    """An app holding a SessionsHost over the catalog it follows, recording the commands it
    sends, those in ``failing`` failing to send, and the marks it shows as the session viewed
    moves."""

    def __init__(self, repository: str | None = None) -> None:
        super().__init__()
        self.sent: list[SessionChoice | SessionChange] = []
        self.failing: set[str] = set()
        self.marks: list[str | None] = []
        self.catalog = CatalogState()
        self.host = SessionsHost(
            self.push_screen,
            self._sent,
            self.catalog,
            Placing(
                lambda workspaces: chosen_workspace(workspaces, repository),
                lambda: self.marks.append(self.host.mark),
            ),
        )

    def follow(self, listed: Catalog) -> None:
        self.catalog.follow(listed)
        self.host.follow()

    def follow_state(self, changed: SessionStateChanged) -> None:
        self.catalog.follow_state(changed)
        self.host.follow_state()

    async def _sent(self, command: SessionChoice | SessionChange, awaited: Awaited) -> bool:
        self.sent.append(command)
        return command.type not in self.failing

    def activation_screens(self) -> list[ChoiceScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, ChoiceScreen)]

    def sessions_screens(self) -> list[SessionsScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, SessionsScreen)]


def test_a_catalog_lists_its_sessions_the_active_one_viewed() -> None:
    app = Host()
    host = app.host
    app.follow(catalog(NOW, OLD))
    assert host.sessions == (NOW, OLD)
    assert host.attached == "now"
    assert host.attached_session == NOW


ELSEWHERE = SessionSummary(id="far", name="elsewhere", active=True)
TWO = Catalog(
    workspaces=(
        WorkspaceEntry(id="w1", label="one", path="/src/one", sessions=(ELSEWHERE,)),
        WorkspaceEntry(id="w2", label="two", path="/src/two", sessions=(NOW, OLD)),
    ),
    agents=(),
    triggers=(),
    lanes=(),
)


def test_the_workspace_at_the_repository_is_opened_on_listing_its_sessions_alone() -> None:
    app = Host("/src/two")
    host = app.host
    app.follow(TWO)
    assert (host.workspace, host.sessions) == ("w2", (NOW, OLD))
    assert host.opening == Attach(workspace="w2", session="now")


def test_without_a_workspace_at_the_repository_the_first_is_opened_on() -> None:
    app = Host("/src/three")
    host = app.host
    app.follow(TWO)
    assert (host.workspace, host.sessions) == ("w1", (ELSEWHERE,))
    assert host.opening == Attach(workspace="w1", session="far")


def test_a_later_catalog_keeps_the_workspace_opened_on() -> None:
    app = Host("/src/two")
    host = app.host
    app.follow(TWO)
    app.follow(TWO.model_copy(update={"workspaces": TWO.workspaces[::-1]}))
    assert host.workspace == "w2"


def test_a_catalog_listing_no_workspace_has_nothing_to_open_on() -> None:
    app = Host()
    host = app.host
    app.follow(Catalog(workspaces=(), agents=(), triggers=(), lanes=()))
    assert (host.workspace, host.sessions, host.opening) == (None, (), None)


async def test_joining_attaches_to_its_active_session_then_is_on_it() -> None:
    app = Host("/src/two")
    app.follow(TWO)
    assert await app.host.join("w1")
    assert app.sent == [Attach(workspace="w1", session="far")]
    assert (app.host.workspace, app.host.attached, app.host.sessions) == ("w1", "far", (ELSEWHERE,))


async def test_a_join_whose_attach_fails_to_send_stays_where_it_was() -> None:
    app = Host("/src/two")
    app.follow(TWO)
    app.host.view(view(OLD))
    app.failing.add("attach")
    assert not await app.host.join("w1")
    assert (app.host.workspace, app.host.attached, app.host.sessions) == ("w2", "old", (NOW, OLD))
    assert app.host.open_session == SessionRef(id="now", name="today")


async def test_joining_a_workspace_without_an_active_session_sends_nothing_and_stays() -> None:
    app = Host("/src/two")
    idle = TWO.workspaces[0].model_copy(update={"sessions": (OLD,)})
    app.follow(TWO.model_copy(update={"workspaces": (idle, TWO.workspaces[1])}))
    assert not await app.host.join("w1")
    assert app.sent == []
    assert (app.host.workspace, app.host.attached) == ("w2", "now")


async def test_a_join_shows_the_mark_of_the_session_it_views_right_away() -> None:
    app = Host("/src/two")
    app.follow(TWO)
    app.host.view(view(OLD))
    await app.host.join("w1")
    assert app.marks == [None]


async def test_a_join_failing_to_send_shows_the_mark_of_the_session_still_viewed() -> None:
    app = Host("/src/two")
    app.follow(TWO)
    app.host.view(view(OLD))
    app.failing.add("attach")
    await app.host.join("w1")
    assert app.marks[-1] == app.host.mark is not None


async def test_a_pick_in_another_workspace_failing_to_attach_stays_where_it_was() -> None:
    app = Host("/src/two")
    async with app.run_test() as pilot:
        app.follow(TWO)
        app.host.open(workspace="w1")
        await pilot.pause()
        app.failing.add("attach")
        app.screen.dismiss((Attach(workspace="w1", session="far"),))
        await pilot.pause()
        assert (app.host.workspace, app.host.attached) == ("w2", "now")


async def test_a_pick_in_another_workspace_attached_stays_there_if_a_later_command_fails() -> None:
    app = Host("/src/two")
    async with app.run_test() as pilot:
        app.follow(TWO)
        app.host.open(workspace="w1")
        await pilot.pause()
        app.failing.add("new_session")
        app.screen.dismiss((Attach(workspace="w1", session="far"), NewSession()))
        await pilot.pause()
        assert (app.host.workspace, app.host.attached) == ("w1", "far")
        assert app.marks == [None]


def test_a_snapshot_says_which_session_is_viewed_over_the_catalog() -> None:
    app = Host()
    host = app.host
    host.view(opened(OLD))
    app.follow(catalog(NOW, OLD))
    assert host.attached_session == OLD


def test_a_catalog_without_the_session_viewed_names_none() -> None:
    app = Host()
    host = app.host
    app.follow(catalog(OLD))
    assert host.attached is None
    assert host.attached_session is None


async def test_a_catalog_while_shown_replaces_the_rows_in_place() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.follow(catalog(NOW))
        app.host.open()
        await pilot.pause()
        app.follow(catalog(NOW, OLD))
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert app.host.shown is screen
        assert screen.sessions == (NOW, OLD)
        assert screen.query_one(OptionList).option_count == 3


async def test_a_catalog_listing_a_session_made_elsewhere_shows_it_while_shown() -> None:
    app = Host()
    made = SessionSummary(id="made", name="made elsewhere")
    async with app.run_test() as pilot:
        app.follow(catalog(NOW, OLD))
        app.host.open()
        await pilot.pause()
        app.follow(catalog(made, NOW, OLD))
        await pilot.pause()
        [screen] = app.sessions_screens()
        options = screen.query_one(OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert any("made elsewhere" in label for label in labels)


async def test_a_state_change_while_shown_updates_its_row() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.follow(catalog(NOW, OLD))
        app.host.open()
        await pilot.pause()
        changed = SessionStateChanged(session="now", active=True, state="running", viewers=1)
        app.follow_state(changed)
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert screen.sessions[0].state == "running"
        assert app.host.sessions[0].state == "running"


async def test_x_on_a_session_no_longer_active_asks_before_deleting_it() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.follow(catalog(NOW, OLD))
        app.host.open()
        await pilot.pause()
        app.follow_state(SessionStateChanged(session="now", active=False, viewers=1, state="idle"))
        await pilot.pause()
        await pilot.press("x")

        assert isinstance(app.screen, ConfirmScreen)
        assert app.sent == []


async def test_opening_twice_shows_one_picker() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.open("resume")
        app.host.open()
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert screen.startup and screen.resume


async def test_dismissing_and_reopening_before_the_callback_keeps_one_picker() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.open()
        await pilot.pause()
        app.screen.dismiss(None)
        app.host.open()
        await pilot.pause()
        app.host.open()
        await pilot.pause()
        app.host.open()
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert app.host.shown is screen


async def test_closing_with_a_choice_sends_it_and_forgets_the_screen() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.open()
        await pilot.pause()
        app.screen.dismiss((NewSession(name="fresh"),))
        await pilot.pause()
        assert app.host.shown is None
        assert app.sent == [NewSession(name="fresh")]


async def test_closing_with_no_commands_sends_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.open()
        await pilot.pause()
        app.screen.dismiss(())
        await pilot.pause()
        assert app.host.shown is None
        assert app.sent == []


async def test_a_command_that_fails_to_send_stops_those_after_it() -> None:
    app = Host()
    app.failing.add("attach")
    async with app.run_test() as pilot:
        app.host.open()
        await pilot.pause()
        app.screen.dismiss((Attach(workspace="w", session="old"), ActivateSession(session="old")))
        await pilot.pause()
        assert app.sent == [Attach(workspace="w", session="old")]


def view(summary: SessionSummary) -> Snapshot:
    """A read-only view of ``summary``'s session, while ``today`` is the active one."""
    update = {"active": False, "open_session": SessionRef(id="now", name="today")}
    return opened(summary).model_copy(update=update)


def test_a_view_marks_the_session_and_a_live_snapshot_clears_the_mark() -> None:
    app = Host()
    host = app.host
    host.view(view(OLD))
    assert not host.live
    assert host.mark == "viewing auth rework; today is idle (enter to switch)"
    host.view(opened(OLD))
    assert host.live
    assert host.open_session is None
    assert host.mark is None


async def test_a_live_session_is_offered_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.view(opened(NOW))
        app.host.offer()
        await pilot.pause()
        assert app.activation_screens() == []


async def test_a_view_naming_no_active_session_is_offered_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.view(opened(OLD).model_copy(update={"active": False}))
        app.host.offer()
        await pilot.pause()
        assert app.activation_screens() == []


async def test_offering_twice_shows_one_offer() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.view(view(OLD))
        app.host.offer()
        app.host.offer()
        await pilot.pause()
        [screen] = app.activation_screens()
        assert app.host.offered is screen
        screen.dismiss(1)  # Just look.
        await pilot.pause()
        assert app.host.offered is None
        assert app.sent == []


async def test_a_view_picked_that_failed_to_send_is_offered_nothing() -> None:
    app = Host()
    app.failing.add("attach")
    async with app.run_test() as pilot:
        app.follow(catalog(NOW, OLD))
        app.host.open()
        await pilot.pause()
        app.screen.dismiss((Attach(workspace="w", session="old"),))
        await pilot.pause()
        app.host.view(view(OLD))
        await pilot.pause()
        assert app.activation_screens() == []


async def test_a_live_snapshot_dismisses_the_offer_shown() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.view(view(OLD))
        app.host.offer()
        await pilot.pause()
        app.host.view(opened(OLD))
        assert app.host.offered is None
        await pilot.pause()
        assert app.activation_screens() == []
        assert app.sent == []


async def test_an_offer_withdrawn_under_another_modal_is_closed_and_sends_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.view(view(OLD))
        app.host.offer()
        await pilot.pause()
        other = ConfirmScreen("Other", "Another modal.", "yes", "no")
        app.push_screen(other)
        await pilot.pause()
        app.host.view(opened(OLD))
        await pilot.pause()
        assert app.activation_screens() == []
        assert app.host.offered is None
        assert app.screen is other
        assert app.sent == []


def test_the_mark_names_the_tasks_the_active_session_runs_without_numbers() -> None:
    app = Host()
    # Opens on the first workspace listed.
    host = SessionsHost(app.push_screen, app._sent, app.catalog)
    app.catalog.follow(catalog(NOW, OLD))
    host.follow()
    host.view(view(OLD))
    run = (Activity(kind="task_run", task_id="t1"),)
    app.catalog.follow_state(RUNNING.model_copy(update={"activity": run}))
    host.follow_state()
    assert host.mark == (
        "viewing auth rework; today is running a task (enter to switch when it's idle)"
    )


async def test_a_held_pick_made_active_itself_is_not_offered_again() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.follow(catalog(NOW, OLD))
        app.follow_state(RUNNING)
        app.host.open()
        await pilot.pause()
        app.screen.dismiss((Attach(workspace="w", session="old"),))
        await pilot.pause()
        app.host.view(view(OLD))
        await pilot.pause()
        [held] = app.activation_screens()

        app.follow_state(SessionStateChanged(session="now", active=False, state="idle", viewers=0))
        app.follow_state(SessionStateChanged(session="old", active=True, state="idle", viewers=1))
        await pilot.pause()
        assert app.activation_screens() == [held]


async def offered_message(app: Host, *catalogs: Catalog) -> str:
    """The message of the offer ``app`` shows for a view of ``auth rework``, once it has
    followed ``catalogs``."""
    async with app.run_test() as pilot:
        for listed in catalogs:
            app.follow(listed)
        app.host.view(view(OLD))
        app.host.offer()
        await pilot.pause()
        [screen] = app.activation_screens()
        return screen.message


async def test_without_a_catalog_the_offer_names_the_active_session_as_the_snapshot_does() -> None:
    assert await offered_message(Host()) == (
        "auth rework is a read-only view; today is the active session, which runs work."
    )


async def test_a_catalog_naming_no_active_session_leaves_the_snapshot_naming_it() -> None:
    idle = NOW.model_copy(update={"active": False})
    assert await offered_message(Host(), catalog(idle, OLD)) == (
        "auth rework is a read-only view; today is the active session, which runs work."
    )


async def test_the_offer_names_the_active_session_as_the_catalog_does_over_the_snapshot() -> None:
    flags = SessionSummary(id="flags", name="flags", active=True)
    assert await offered_message(Host(), catalog(OLD, flags)) == (
        "auth rework is a read-only view; flags is the active session, which runs work."
    )
