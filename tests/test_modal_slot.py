"""The ModalSlot holding one modal at a time and telling a closed one from a stale one."""

from collections.abc import Awaitable, Callable

from textual.screen import ModalScreen

from orbit.tui.modal import ModalSlot

Closed = Callable[[int | None], Awaitable[None]]


class Screen(ModalScreen[int]):
    """A modal recording what it was dismissed with."""

    def __init__(self) -> None:
        super().__init__()
        self.dismissed: list[int | None] = []

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
