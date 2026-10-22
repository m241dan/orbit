"""The ModalSlot holding one modal at a time and telling a closed one from a stale one, and
closing a modal wherever it is in the screen stack."""

from collections.abc import Awaitable, Callable

from textual.app import App
from textual.screen import ModalScreen

from orbit.tui.modal import ModalSlot, close_modal

Closed = Callable[[int | None], Awaitable[None]]


class Screen(ModalScreen[int]):
    """A modal on top, recording what it was dismissed with."""

    def __init__(self) -> None:
        super().__init__()
        self.dismissed: list[int | None] = []

    @property
    def is_active(self) -> bool:
        return True

    def dismiss(self, result: int | None = None) -> None:  # type: ignore[override]
        self.dismissed.append(result)


def recording_slot() -> tuple[ModalSlot[Screen, int], list[tuple[Screen, Closed]]]:
    """A ModalSlot recording what it pushes."""
    pushed: list[tuple[Screen, Closed]] = []
    return ModalSlot(lambda screen, closed: pushed.append((screen, closed))), pushed


async def test_opening_pushes_the_screen_and_shows_it() -> None:
    slot, pushed = recording_slot()
    screen = Screen()
    slot.open(screen, record([]))
    assert pushed[0][0] is screen
    assert slot.showing is screen


async def test_a_screen_closing_itself_clears_it_and_passes_on_its_result() -> None:
    slot, pushed = recording_slot()
    seen: list[tuple[int | None, Screen | None]] = []

    async def closed(result: int | None) -> None:
        seen.append((result, slot.showing))

    slot.open(Screen(), closed)
    [(_, callback)] = pushed
    await callback(2)
    assert slot.showing is None
    assert seen == [(2, None)]  # Cleared before the result is passed on.


async def test_closing_dismisses_the_screen_shown_with_the_result_and_clears_it() -> None:
    slot, _ = recording_slot()
    screen = Screen()
    slot.open(screen, record([]))
    slot.close(1)
    assert screen.dismissed == [1]
    assert slot.showing is None


async def test_closing_with_none_shown_does_nothing() -> None:
    slot, _ = recording_slot()
    slot.close(1)
    assert slot.showing is None


async def test_a_closed_screens_late_callback_passes_nothing_on_and_leaves_the_newer() -> None:
    slot, pushed = recording_slot()
    results: list[int | None] = []
    slot.open(Screen(), record(results))
    slot.close(None)
    newer = Screen()
    slot.open(newer, record([]))
    [(_, stale), _] = pushed
    await stale(None)
    assert results == []
    assert slot.showing is newer


def record(results: list[int | None]) -> Closed:
    """A callback appending each result to ``results``."""

    async def closed(result: int | None) -> None:
        results.append(result)

    return closed


class Modal(ModalScreen[int]):
    """A modal with nothing in it."""


async def test_closing_a_modal_on_top_dismisses_it_calling_back_with_the_result() -> None:
    app = App[None]()
    results: list[int | None] = []
    async with app.run_test() as pilot:
        screen = Modal()
        app.push_screen(screen, results.append)
        await pilot.pause()
        close_modal(screen, 1)
        await pilot.pause()
        assert screen not in app.screen_stack
        assert results == [1]


async def test_closing_a_modal_under_another_removes_it_without_calling_back() -> None:
    app = App[None]()
    results: list[int | None] = []
    async with app.run_test() as pilot:
        screen, other = Modal(), Modal()
        app.push_screen(screen, results.append)
        app.push_screen(other)
        await pilot.pause()
        close_modal(screen, 1)
        await pilot.pause()
        assert app.screen_stack == [app.screen_stack[0], other]
        assert not screen.is_attached
        assert results == []
        other.dismiss(None)
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_closing_a_modal_closed_already_leaves_the_one_on_top() -> None:
    app = App[None]()
    async with app.run_test() as pilot:
        screen, other = Modal(), Modal()
        app.push_screen(screen)
        await pilot.pause()
        screen.dismiss(1)
        app.push_screen(other)
        close_modal(screen, None)
        await pilot.pause()
        assert app.screen is other
        assert screen not in app.screen_stack
