from textual.app import App, ComposeResult
from textual.widgets import Static

from orbit.tui.view import SPINNER_FPS
from orbit.tui.widgets import Ticker


class TickerApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.ticks = 0

    def compose(self) -> ComposeResult:
        yield Static()

    def tick(self) -> None:
        self.ticks += 1


async def test_a_new_ticker_is_not_ticking() -> None:
    app = TickerApp()
    async with app.run_test():
        ticker = Ticker(app.query_one(Static), app.tick)
        assert not ticker.ticking
        ticker.keep(False)
        assert not ticker.ticking


async def test_keeping_it_running_ticks_at_the_spinner_rate() -> None:
    app = TickerApp()
    async with app.run_test() as pilot:
        ticker = Ticker(app.query_one(Static), app.tick)
        ticker.keep(True)
        assert ticker.ticking
        await pilot.pause(3 / SPINNER_FPS)
        assert app.ticks


async def test_keeping_it_running_again_starts_no_second_timer() -> None:
    app = TickerApp()
    async with app.run_test() as pilot:
        owner = app.query_one(Static)
        ticker = Ticker(owner, app.tick)
        ticker.keep(True)
        ticker.keep(True)
        ticker.keep(False)
        assert not ticker.ticking
        ticks = app.ticks
        await pilot.pause(3 / SPINNER_FPS)
        assert app.ticks == ticks
