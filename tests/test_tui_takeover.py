"""The offer of a freed repository: a modal to take it over or stay read-only, as the app asks
it and as the host shows it in a bare app."""

import pytest
from textual.app import App
from textual.widgets import Static

from orbit.protocol import (
    STORAGE_FAILED,
    Command,
    ErrorOccurred,
    OwnershipOffered,
    OwnershipTaken,
    TakeOwnership,
)
from orbit.tui import OrbitApp
from orbit.tui.storage_halt import StorageHaltScreen
from orbit.tui.takeover import TakeoverHost
from orbit.tui.view.confirm import TAKE_OVER_MESSAGE, TAKE_OVER_TITLE
from orbit.tui.view.outcomes import Awaited
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


class TakeoverApp(App[None]):
    """A bare app showing a TakeoverHost's offer, recording the commands it sends."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Command] = []
        self.host = TakeoverHost(self.push_screen, self._send)

    async def _send(self, command: Command, awaited: Awaited) -> None:
        self.sent.append(command)


@pytest.mark.parametrize("key", ["n", "escape"])
async def test_staying_read_only_sends_the_choice(key: str) -> None:
    app = TakeoverApp()
    async with app.run_test() as pilot:
        app.host.offer.offered()
        await pilot.pause()
        await pilot.press(key)
        assert app.sent == [TakeOwnership(take=False)]
        assert not app.host.modal_open


async def test_an_offer_settled_closes_the_modal_unanswered() -> None:
    app = TakeoverApp()
    async with app.run_test() as pilot:
        app.host.offer.offered()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and app.host.modal_open
        app.host.offer.settled()
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen) and not app.host.modal_open
        assert app.sent == []
        app.host.offer.offered()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
