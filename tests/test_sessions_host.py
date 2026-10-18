"""The SessionsHost tracking whether the list of sessions is shown, and what it opens as."""

from textual.app import App
from textual.widgets import OptionList

from orbit.protocol import NewSession, SessionsListed, SessionSummary
from orbit.tui.sessions import SessionChange, SessionChoice, SessionsHost, SessionsScreen

OLD = SessionSummary(id="old", name="auth rework")
NEW = SessionSummary(id="new", name="today", current=True)
STARTUP = SessionsListed(sessions=(OLD,), reason="startup")


class Host(App[None]):
    """An app holding a SessionsHost, recording the commands it sends."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SessionChoice | SessionChange] = []
        self.host = SessionsHost(self.push_screen, self._sent)

    async def _sent(self, command: SessionChoice | SessionChange) -> None:
        self.sent.append(command)

    def sessions_screens(self) -> list[SessionsScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, SessionsScreen)]


async def test_a_listing_while_shown_replaces_the_rows_in_place() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(STARTUP)
        await pilot.pause()
        app.host.show(SessionsListed(sessions=(OLD, NEW), reason="refreshed"))
        await pilot.pause()
        [screen] = app.sessions_screens()
        assert app.host.shown is screen
        assert screen.sessions == (OLD, NEW)
        assert screen.query_one(OptionList).option_count == 3


async def test_a_refreshed_listing_is_ignored_once_closed() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(SessionsListed(sessions=(OLD,), reason="refreshed"))
        await pilot.pause()
        assert app.sessions_screens() == []
        assert app.host.shown is None


async def test_a_startup_listing_opens_in_startup_mode_only_before_asked() -> None:
    first, second = Host(), Host()
    async with first.run_test() as pilot:
        first.host.show(STARTUP)
        await pilot.pause()
        assert first.host.shown is not None and first.host.shown.startup
    async with second.run_test() as pilot:
        second.host.asked()
        second.host.show(STARTUP)
        await pilot.pause()
        assert second.host.shown is not None and not second.host.shown.startup


async def test_closing_with_a_choice_sends_it_and_forgets_the_screen() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(STARTUP)
        await pilot.pause()
        app.screen.dismiss(NewSession(name="fresh"))
        await pilot.pause()
        assert app.host.shown is None
        assert app.sent == [NewSession(name="fresh")]


async def test_closing_with_none_sends_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(SessionsListed(sessions=(OLD,), reason="asked"))
        await pilot.pause()
        app.screen.dismiss(None)
        await pilot.pause()
        assert app.host.shown is None
        assert app.sent == []
