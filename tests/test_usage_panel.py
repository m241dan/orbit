from datetime import UTC, datetime

from textual.widgets import Static

from orbit.protocol import UsageChanged, UsageLimit
from orbit.tui import OrbitApp
from orbit.tui.widgets import UsagePanel
from tests.test_task_list import feed
from tests.test_tui import FakeSession

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


async def test_panel_is_hidden_until_usage_is_reported() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        panel = app.query_one(UsagePanel)
        assert panel.border_title == "Usage"
        assert not panel.display
        assert not panel.can_focus
        # The minute's redraw has nothing to show yet.
        panel.tick()
        assert not panel.display
        assert rows(app) == [""]


async def test_panel_shows_usage_once_reported() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        panel = app.query_one(UsagePanel)
        panel.now = lambda: NOW
        await feed(session, pilot, USAGE)
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


async def test_panel_redraws_its_bar_to_fit_a_resize() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(90, 30)) as pilot:
        app.query_one(UsagePanel).now = lambda: NOW
        await feed(session, pilot, USAGE)
        narrow = len(rows(app)[1])
        await pilot.resize_terminal(150, 30)
        await pilot.pause()
        wide = rows(app)[1]
        assert len(wide) > narrow
        assert len(wide) == app.query_one(UsagePanel).content_region.width


async def test_panel_redraws_its_reset_times_as_the_clock_moves() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        panel = app.query_one(UsagePanel)
        now = [NOW]
        panel.now = lambda: now[0]
        await feed(session, pilot, USAGE)
        now[0] = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        panel.tick()
        assert rows(app)[0] == "Session  50%  resets Fri 3:40pm"
