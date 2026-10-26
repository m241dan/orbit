"""The modal asking whether a consultation past its asker's hourly limit may run: its text, the
app opening it and sending the user's decision, and closing it when another frontend decides."""

from textual.widgets import Static

from orbit.protocol import PLANNER, ApprovalDecided, ApprovalRequested, DecideApproval
from orbit.tui import OrbitApp
from orbit.tui.approvals import ApprovalScreen
from orbit.tui.view.approvals import approval_text
from tests.tui_fakes import FakeSession, feed_events

REQUEST = ApprovalRequested(
    consultation=7, asker=PLANNER, consultant="experimenter", title="Check X", asked=6
)


def test_the_modal_names_both_roles_the_first_line_and_the_count_then_the_choices() -> None:
    assert approval_text(REQUEST) == (
        "The planner wants to ask the experimenter: 'Check X'. It has asked 6 times this hour.\n"
        "\n"
        "a  Allow\n"
        "h  Allow for the rest of the hour\n"
        "d  Deny"
    )


def test_one_consultation_is_counted_as_one_time() -> None:
    once = REQUEST.model_copy(update={"asked": 1})
    assert approval_text(once).startswith(
        "The planner wants to ask the experimenter: 'Check X'. It has asked 1 time this hour."
    )


async def test_a_request_opens_the_modal_showing_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, REQUEST)
        screen = app.screen
        assert isinstance(screen, ApprovalScreen)
        assert str(screen.query_one("#approval-body", Static).render()) == approval_text(REQUEST)
        assert session.commands == []


async def decides(key: str) -> list[object]:
    """The commands the app sends as the user presses ``key`` on the modal."""
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, REQUEST)
        await pilot.press(key)
        await pilot.pause()
        assert not isinstance(app.screen, ApprovalScreen)
    return list(session.commands)


async def test_a_allows_it() -> None:
    assert await decides("a") == [DecideApproval(consultation=7, decision="allow")]


async def test_h_allows_it_and_the_rest_of_the_hour() -> None:
    assert await decides("h") == [DecideApproval(consultation=7, decision="allow_hour")]


async def test_d_denies_it() -> None:
    assert await decides("d") == [DecideApproval(consultation=7, decision="deny")]


async def test_escape_leaves_it_waiting() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, REQUEST)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ApprovalScreen)


async def test_another_frontends_decision_closes_it_sending_nothing() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, REQUEST)
        await feed_events(session, pilot, ApprovalDecided(consultation=7, decision="deny"))
        assert not isinstance(app.screen, ApprovalScreen)
        assert session.commands == []
