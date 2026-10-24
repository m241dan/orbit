"""The usage panel: the app showing usage once it's reported, beside the other panels, and the
panel alone hidden until then, fitting its width and following the clock."""

from datetime import UTC, datetime

from textual.app import App, ComposeResult
from textual.widgets import Static

from orbit.protocol import UsageChanged, UsageLimit
from orbit.tui import OrbitApp
from orbit.tui.panels import UsagePanel
from tests.tui_fakes import FakeSession, feed_events

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
USAGE = UsageChanged(
    limits=(
        UsageLimit(
            label="Session", percent=50, resets_at=datetime(2026, 10, 16, 15, 40, tzinfo=UTC)
        ),
    )
)


def rows(app: OrbitApp) -> list[str]:
    return str(app.query_one(UsagePanel).query_one(Static).render()).split("\n")


async def test_panel_shows_usage_once_reported() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        panel = app.query_one(UsagePanel)
        panel.now = lambda: NOW
        await feed_events(session, pilot, USAGE)
        assert panel.display
        header, bar = rows(app)
        assert header == "Session  50%  resets 3:40pm"
        assert len(bar) == panel.content_region.width
        assert bar.count("█") == round(len(bar) / 2)


async def test_panel_sits_between_the_verification_steps_and_the_agents() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        ids = [child.id for child in app.query_one("#side").children]
        assert ids.index("verification-steps") + 1 == ids.index("usage")
        assert ids.index("usage") + 1 == ids.index("agents")


class PanelApp(App[None]):
    """A bare app around a usage panel."""

    def compose(self) -> ComposeResult:
        yield UsagePanel(id="usage")

    @property
    def panel(self) -> UsagePanel:
        return self.query_one(UsagePanel)


def panel_rows(app: PanelApp) -> list[str]:
    return str(app.panel.query_one(Static).render()).split("\n")


async def test_panel_is_hidden_until_usage_is_reported() -> None:
    app = PanelApp()
    async with app.run_test():
        panel = app.panel
        assert panel.border_title == "Usage"
        assert not panel.display
        assert not panel.can_focus
        # The minute's redraw has nothing to show yet.
        panel.tick()
        assert not panel.display
        assert panel_rows(app) == [""]


async def test_panel_redraws_its_bar_to_fit_a_resize() -> None:
    app = PanelApp()
    async with app.run_test(size=(90, 30)) as pilot:
        app.panel.now = lambda: NOW
        app.panel.show(USAGE)
        await pilot.pause()
        narrow = len(panel_rows(app)[1])
        await pilot.resize_terminal(150, 30)
        await pilot.pause()
        wide = panel_rows(app)[1]
        assert len(wide) > narrow
        assert len(wide) == app.panel.content_region.width


async def test_panel_redraws_its_reset_times_as_the_clock_moves() -> None:
    app = PanelApp()
    async with app.run_test(size=(120, 30)) as pilot:
        panel = app.panel
        now = [NOW]
        panel.now = lambda: now[0]
        panel.show(USAGE)
        await pilot.pause()
        now[0] = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        panel.tick()
        assert panel_rows(app)[0] == "Session  50%  resets Fri 3:40pm"
