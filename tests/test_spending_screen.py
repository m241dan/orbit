"""u on a conversational role's tab opens the breakdown of usage by role, and the
transcript's corner shows the shown tab's usage: the app, with Textual."""

from textual.containers import VerticalScroll

from orbit.protocol import Usage
from orbit.tui import OrbitApp
from orbit.tui.transcript import Transcript
from orbit.tui.view.spend import SPENDING_TITLE
from orbit.tui.widgets import TextScreen
from tests.tui_fakes import SNAPSHOT, FakeSession, feed_events

SPENT = SNAPSHOT.model_copy(update={"role_spent": {"planner": Usage(cost_usd=0.42)}})


async def test_u_on_the_planners_tab_opens_the_breakdown_and_escape_closes_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SPENT)
        assert app.query_one(Transcript).border_subtitle == "session $2.00"
        await pilot.press("ctrl+t")
        assert app.query_one(Transcript).border_subtitle == "$0.42"
        await pilot.press("u")
        screen = app.screen
        assert isinstance(screen, TextScreen)
        box = screen.query_one(VerticalScroll)
        assert str(box.border_title) == SPENDING_TITLE
        assert str(box.query_one("#text-body").render()) == "planner: $0.42 itself"
        await pilot.press("escape")
        assert not isinstance(app.screen, TextScreen)
