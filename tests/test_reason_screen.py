"""f on the task list opens a modal saying in full why the highlighted task ended: the app
opening it, and the screen alone scrolling, copying and closing."""

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.pilot import Pilot

from orbit.protocol import TasksChanged, TaskSpec, TaskState, TaskStatus
from orbit.tui import OrbitApp
from orbit.tui.reason import ReasonScreen
from orbit.tui.view.tasks import REASON_HINT, reason_text, reason_title
from tests.tui_fakes import FakeSession, feed_events, open_task_list

LONG = "\n".join(f"line {n}" for n in range(100))


def task(
    id: str = "1",
    status: TaskStatus = "unverified",
    reason: str = "Unit tests failed",
    detail: str = "",
    fixes: str | None = None,
) -> TaskState:
    return TaskState(
        id=id,
        task=TaskSpec(prompt=f"task {id}"),
        status=status,
        reason=reason,
        reason_detail=detail,
        fixes=fixes,
    )


def notes(app: App[None], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The toasts ``app`` shows from now on."""
    shown: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **_: shown.append(message))
    return shown


def reason_screen(app: App[None]) -> ReasonScreen:
    screen = app.screen
    assert isinstance(screen, ReasonScreen)
    return screen


def shown(app: OrbitApp) -> tuple[str, str, str]:
    """The open modal's title, subtitle and body."""
    box = reason_screen(app).query_one(VerticalScroll)
    body = str(box.query_one("#text-body").render())
    return str(box.border_title), str(box.border_subtitle), body


async def opened(session: FakeSession, pilot: Pilot[None], *tasks: TaskState) -> None:
    """Show ``tasks``, activate the list and press f on the first one."""
    await feed_events(session, pilot, TasksChanged(tasks=tasks))
    await open_task_list(pilot)
    await pilot.press("f")


@pytest.mark.parametrize("status", ["unverified", "gave_up", "error"])
def test_a_task_that_ended_badly_is_titled_failed(status: TaskStatus) -> None:
    assert reason_title(task(status=status), "3") == "Why task 3 failed"


def test_a_cancelled_task_is_titled_stopped() -> None:
    assert reason_title(task(status="cancelled"), "1.2") == "Why task 1.2 stopped"


def test_reason_text_puts_the_detail_after_a_blank_line() -> None:
    assert reason_text(task(detail="out\nmore")) == "Unit tests failed\n\nout\nmore"


def test_reason_text_without_a_detail_is_just_the_reason() -> None:
    assert reason_text(task()) == "Unit tests failed"


async def test_f_on_a_failed_task_opens_its_reason() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await opened(session, pilot, task(detail="$ pytest\n1 failed"))
        assert shown(app) == (
            "Why task 1 failed",
            REASON_HINT,
            "Unit tests failed\n\n$ pytest\n1 failed",
        )


async def test_f_on_a_cancelled_fix_says_it_stopped_with_its_number() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        stopped = task("2", status="cancelled", reason="Cancelled by the user", fixes="1")
        await feed_events(session, pilot, TasksChanged(tasks=(task("1"), stopped)))
        await open_task_list(pilot)
        await pilot.press("j", "f")
        assert shown(app)[0] == "Why task 1.1 stopped"


async def test_f_does_nothing_on_a_task_without_a_reason_or_the_new_task_row() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await opened(session, pilot, task(status="pending", reason=""))
        await pilot.press("j", "f")
        assert not isinstance(app.screen, ReasonScreen)


async def test_it_keeps_the_task_as_opened_when_the_board_changes() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await opened(session, pilot, task())
        await feed_events(session, pilot, TasksChanged(tasks=()))
        assert shown(app)[2] == "Unit tests failed"


class ReasonApp(App[None]):
    """A bare app showing why ``state``, task 1, ended."""

    AUTO_FOCUS = None  # As OrbitApp's, which the screen inherits.

    def __init__(self, state: TaskState) -> None:
        super().__init__()
        self.state = state

    def on_mount(self) -> None:
        self.push_screen(ReasonScreen(self.state, "1"))


async def test_y_copies_the_reason_and_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ReasonApp(task(detail="out"))
    toasts = notes(app, monkeypatch)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test() as pilot:
        await pilot.press("y")
        assert copied == ["Unit tests failed\n\nout"] and toasts == ["Copied 3 lines"]


async def test_j_k_and_the_arrows_scroll_a_long_detail() -> None:
    app = ReasonApp(task(detail=LONG))
    async with app.run_test(size=(80, 30)) as pilot:
        box = reason_screen(app).query_one(VerticalScroll)
        await pilot.press("j", "j", "down")
        assert box.scroll_y == 3
        await pilot.press("k", "up")
        assert box.scroll_y == 1


async def test_escape_closes_it() -> None:
    app = ReasonApp(task())
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert not isinstance(app.screen, ReasonScreen)
