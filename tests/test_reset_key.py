"""ctrl+n: reset the planner, after a confirmation, and rule off its conversation."""

from textual.widgets import Static

from orbit.protocol import PLANNER, AgentStarted, ConversationReset, ResetConversation
from orbit.tui import OrbitApp
from orbit.tui.transcript import Transcript
from orbit.tui.view.common import EntryKind
from orbit.tui.view.confirm import reset_message, reset_title
from orbit.tui.view.rules import reset_entry
from orbit.tui.widgets import ConfirmScreen, PickerScreen
from tests.tui_fakes import FakeSession, entries


async def test_ctrl_n_asks_then_resets_the_planner() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        assert session.commands == []
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        assert session.commands == [ResetConversation(agent=PLANNER)]
        assert not isinstance(app.screen, ConfirmScreen)


async def test_ctrl_n_over_another_modal_does_not_ask() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await app.push_screen(PickerScreen("Model", ["default"]))
        await pilot.press("ctrl+n")
        assert isinstance(app.screen, PickerScreen)
        assert not app.check_action("reset_planner", ())


async def test_the_reset_confirmation_shows_its_question_and_keys() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        body = app.screen.query_one("#confirm-message", Static)
        assert body.border_title == reset_title(PLANNER)
        assert body.border_subtitle == "y reset · n/esc cancel"
        assert str(body.render()) == reset_message(PLANNER)


def test_the_reset_rule_is_a_muted_rule_naming_the_role() -> None:
    assert reset_entry(PLANNER) == ("planner reset", "rule")
    assert reset_entry("architect") == ("architect reset", "rule")


async def test_a_reset_draws_a_rule_under_the_planner_tab_and_all() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await session.queue.put(AgentStarted(agent_id="agent-1", role=PLANNER, task_id="plan"))
        await session.queue.put(ConversationReset(agent=PLANNER))
        rule: tuple[str, EntryKind] = ("planner reset", "rule")
        while rule not in entries(app):
            await pilot.pause()
        planner = app.query_one(Transcript).entries(PLANNER)
        assert [(e.text, e.kind) for e in planner][-1] == rule
