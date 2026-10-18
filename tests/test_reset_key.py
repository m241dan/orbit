"""ctrl+n: reset the planner, after a confirmation, and rule off its conversation."""

import pytest
from textual.widgets import Static

from orbit.protocol import PLANNER, AgentStarted, ConversationReset, ResetConversation
from orbit.tui import OrbitApp
from orbit.tui.transcript import Transcript
from orbit.tui.view import EntryKind, planner_reset_entry
from orbit.tui.widgets import RESET_MESSAGE, RESET_TITLE, ConfirmScreen, PickerScreen
from tests.test_tui import FakeSession, entries


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


@pytest.mark.parametrize("key", ["n", "escape"])
async def test_declining_the_reset_sends_nothing(key: str) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n", key)
        assert not isinstance(app.screen, ConfirmScreen)
        assert session.commands == []


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
        assert body.border_title == RESET_TITLE
        assert body.border_subtitle == "y reset · n/esc cancel"
        assert str(body.render()) == RESET_MESSAGE


def test_the_reset_rule_is_a_muted_rule() -> None:
    assert planner_reset_entry() == ("planner reset", "rule")


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
