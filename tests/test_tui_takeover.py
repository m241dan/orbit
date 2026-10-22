"""The offer of a freed repository: a modal to take it over or stay read-only."""

import pytest
from textual.widgets import Static

from orbit.protocol import (
    STORAGE_FAILED,
    CommandAccepted,
    CommandRefused,
    ErrorOccurred,
    Event,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    TakeOwnership,
)
from orbit.tui import OrbitApp
from orbit.tui.storage_halt import StorageHaltScreen
from orbit.tui.view.confirm import TAKE_OVER_MESSAGE, TAKE_OVER_TITLE
from orbit.tui.widgets import ChatPrompt, ConfirmScreen
from tests.tui_fakes import SNAPSHOT, FakeSession, feed_events

OFFERING = SNAPSHOT.model_copy(update={"read_only": True, "ownership_offered": True})


async def test_an_offer_asks_and_taking_over_sends_the_choice() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OwnershipOffered())
        assert isinstance(app.screen, ConfirmScreen)
        body = app.screen.query_one("#confirm-message", Static)
        assert body.border_title == TAKE_OVER_TITLE
        assert body.border_subtitle == "y take over · n/esc stay read-only"
        assert str(body.render()) == TAKE_OVER_MESSAGE
        await pilot.press("y")
        assert session.commands == [TakeOwnership(take=True)]
        assert not isinstance(app.screen, ConfirmScreen)


@pytest.mark.parametrize("key", ["n", "escape"])
async def test_staying_read_only_sends_the_choice(key: str) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OwnershipOffered())
        await pilot.press(key)
        assert session.commands == [TakeOwnership(take=False)]


async def test_owning_the_repository_clears_the_read_only_mark() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"read_only": True}))
        await feed_events(session, pilot, OwnershipTaken())
        assert app.query_one(ChatPrompt).border_title == "Prompt · planner · busy"


async def test_a_snapshot_with_an_offer_open_asks() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OFFERING)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        assert session.commands == [TakeOwnership(take=True)]


async def test_an_offer_already_shown_isnt_asked_again() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OwnershipOffered(), OFFERING, OwnershipOffered())
        assert sum(isinstance(screen, ConfirmScreen) for screen in app.screen_stack) == 1


@pytest.mark.parametrize("settled", [OwnershipTaken(), OwnershipDeclined(), SNAPSHOT])
async def test_an_offer_settled_elsewhere_closes_the_modal_unanswered(settled: Event) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OFFERING, settled)
        assert not isinstance(app.screen, ConfirmScreen)
        assert session.commands == []

        await feed_events(session, pilot, OwnershipOffered())
        assert isinstance(app.screen, ConfirmScreen)


def refusal(session: FakeSession) -> CommandRefused:
    return CommandRefused(
        command_id=session.envelopes[-1].command_id, code="invalid", message="Not now."
    )


@pytest.mark.parametrize(("key", "take"), [("y", True), ("n", False)])
async def test_a_refused_answer_asks_again(key: str, take: bool) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OwnershipOffered())
        await pilot.press(key)
        assert not isinstance(app.screen, ConfirmScreen)
        await feed_events(session, pilot, refusal(session))
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press(key)
        assert session.commands == [TakeOwnership(take=take), TakeOwnership(take=take)]


@pytest.mark.parametrize("settled", [OwnershipTaken(), OwnershipDeclined()])
async def test_a_refusal_after_the_offer_settled_doesnt_ask_again(settled: Event) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OFFERING)
        await pilot.press("y")
        await feed_events(session, pilot, settled, refusal(session))
        assert not isinstance(app.screen, ConfirmScreen)


async def test_an_accepted_answer_leaves_no_modal_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OwnershipOffered())
        await pilot.press("y")
        accepted = CommandAccepted(command_id=session.envelopes[-1].command_id)
        await feed_events(session, pilot, accepted)
        assert not isinstance(app.screen, ConfirmScreen)


async def test_an_offer_settled_under_a_storage_halt_leaves_no_modal_once_it_lifts() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    halt = ErrorOccurred(code=STORAGE_FAILED, message="Orbit stopped this session.")
    async with app.run_test() as pilot:
        await feed_events(session, pilot, OFFERING, halt, OwnershipTaken())
        assert isinstance(app.screen, StorageHaltScreen)
        assert not any(isinstance(screen, ConfirmScreen) for screen in app.screen_stack)
        await feed_events(session, pilot, SNAPSHOT)
        assert not any(isinstance(screen, ConfirmScreen) for screen in app.screen_stack)
        assert not isinstance(app.screen, StorageHaltScreen)
        assert session.commands == []
