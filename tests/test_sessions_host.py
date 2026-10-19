"""The SessionsHost keeping the sessions listed, and showing them in the picker."""

from textual.app import App
from textual.widgets import OptionList

from orbit.protocol import (
    ActivateSession,
    Attach,
    Catalog,
    NewSession,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Snapshot,
    WorkspaceEntry,
)
from orbit.tui.sessions import (
    ActivationScreen,
    SessionChange,
    SessionChoice,
    SessionsHost,
    SessionsScreen,
)
from tests.test_tui import opened

OLD = SessionSummary(id="old", name="auth rework")
NOW = SessionSummary(id="now", name="today", current=True, active=True, viewers=1)


def catalog(*sessions: SessionSummary) -> Catalog:
    workspace = WorkspaceEntry(id="w", label="project", sessions=sessions)
    return Catalog(workspaces=(workspace,), agents=(), triggers=(), lanes=())


class Host(App[None]):
    """An app holding a SessionsHost, recording the commands it sends; those in ``failing``
    fail to send."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SessionChoice | SessionChange] = []
        self.failing: set[str] = set()
        self.host = SessionsHost(self.push_screen, self._sent)

    async def _sent(self, command: SessionChoice | SessionChange) -> bool:
        self.sent.append(command)
        return command.type not in self.failing

    def activation_screens(self) -> list[ActivationScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, ActivationScreen)]

    def sessions_screens(self) -> list[SessionsScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, SessionsScreen)]


def test_a_catalog_lists_its_sessions_the_active_one_viewed() -> None:
    host = Host().host
    host.follow(catalog(NOW, OLD))
    assert host.sessions == (NOW, OLD)
    assert host.attached == "now"
    assert host.attached_session == NOW


def test_a_snapshot_says_which_session_is_viewed_over_the_catalog() -> None:
    host = Host().host
    host.view(opened(OLD))
    host.follow(catalog(NOW, OLD))
    assert host.attached_session == OLD


def test_a_catalog_without_the_session_viewed_names_none() -> None:
    host = Host().host
    host.follow(catalog(OLD))
    assert host.attached is None
    assert host.attached_session is None


async def test_a_catalog_while_shown_replaces_the_rows_in_place() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.follow(catalog(NOW))
        app.host.open()
        await pilot.pause()
        app.host.follow(catalog(NOW, OLD))
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert app.host.shown is screen
        assert screen.sessions == (NOW, OLD)
        assert screen.query_one(OptionList).option_count == 3


async def test_a_state_change_while_shown_updates_its_row() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.follow(catalog(NOW, OLD))
        app.host.open()
        await pilot.pause()
        changed = SessionStateChanged(session="now", active=True, state="running", viewers=1)
        app.host.follow_state(changed)
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert screen.sessions[0].state == "running"
        assert app.host.sessions[0].state == "running"


async def test_opening_twice_shows_one_picker() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.open("resume")
        app.host.open()
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert screen.startup and screen.resume


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
        app.screen.dismiss((Attach(session="old"), ActivateSession(session="old")))
        await pilot.pause()
        assert app.sent == [Attach(session="old")]


def view(summary: SessionSummary) -> Snapshot:
    """A read-only view of ``summary``'s session, while ``today`` is the active one."""
    update = {"active": False, "open_session": SessionRef(id="now", name="today")}
    return opened(summary).model_copy(update=update)


def test_a_view_marks_the_session_and_a_live_snapshot_clears_the_mark() -> None:
    host = Host().host
    host.view(view(OLD))
    assert not host.live
    assert host.mark == "viewing auth rework, not the active session (enter to switch)"
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
        screen.dismiss(False)
        await pilot.pause()
        assert app.host.offered is None
        assert app.sent == []


async def test_a_view_picked_that_failed_to_send_is_offered_nothing() -> None:
    app = Host()
    app.failing.add("attach")
    async with app.run_test() as pilot:
        app.host.follow(catalog(NOW, OLD))
        app.host.open()
        await pilot.pause()
        app.screen.dismiss((Attach(session="old"),))
        await pilot.pause()
        app.host.view(view(OLD))
        await pilot.pause()
        assert app.activation_screens() == []
