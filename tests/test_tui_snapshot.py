"""The TUI replacing what it shows with a snapshot of the open session, as the app shows it; what
a snapshot shows is checked in test_snapshots."""

from orbit.protocol import PLANNER
from orbit.tui import OrbitApp
from orbit.tui.transcript import Transcript
from tests.tui_fakes import SNAPSHOT, FakeSession, call, feed_events, result, selected


async def test_a_snapshot_moves_the_selection_off_the_detail_pane_it_closes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, call(PLANNER, "c1"), result(PLANNER, "c1", "x"))
        await pilot.press("ctrl+t", "k", "enter", "enter", "tab")
        transcript = app.query_one(Transcript)
        assert transcript.detail.opened and selected(app) == ["detail"]
        await feed_events(session, pilot, SNAPSHOT)
        assert not transcript.detail.opened and selected(app) == ["transcript"]
        await pilot.press("enter")
        assert app.focused is transcript.shown
