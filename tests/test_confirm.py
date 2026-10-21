"""The shared confirmation, which runs its action only once the user answers yes."""

import pytest
from textual.app import App

from orbit.tui.widgets import confirm


@pytest.mark.parametrize(("key", "ran"), [("y", [True]), ("n", []), ("escape", [])])
async def test_a_confirmation_runs_its_action_only_on_yes(key: str, ran: list[bool]) -> None:
    app: App[None] = App()
    done: list[bool] = []

    async def then() -> None:
        done.append(True)

    async with app.run_test() as pilot:
        confirm(app.push_screen, "Remove", "Remove it?", "remove", then)
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert done == ran
