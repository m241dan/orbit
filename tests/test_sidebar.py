import pytest
from rich.text import Text
from textual.widgets.option_list import Option

from orbit.protocol import TasksChanged
from orbit.tui import OrbitApp
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view.common import SPINNER_FPS, spinner_frame
from orbit.tui.widgets import ChatPrompt
from tests.tui_fakes import FakeSession, feed_events, selected, task


def sidebar_shown(app: OrbitApp) -> bool:
    return app.query_one("#side").display


async def test_ctrl_b_hides_the_sidebar_and_the_conversation_takes_the_full_width() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(120, 41)) as pilot:
        await pilot.press("ctrl+b")
        assert not sidebar_shown(app)
        assert app.query_one(Transcript).region.width == 120
        assert app.query_one(ChatPrompt).region.width == 120

        await pilot.press("ctrl+b")
        assert sidebar_shown(app)
        assert app.query_one(Transcript).region.width == 81


async def test_sidebar_shows_in_the_footer() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(200, 30)):
        keys = {b.binding.description for b in app.active_bindings.values() if b.binding.show}
        assert "Sidebar" in keys


@pytest.mark.parametrize(
    ("keys", "expected"),
    [("kl", "transcript"), ("l", "prompt")],
)
async def test_hjkl_skip_sidebar_windows_while_it_is_hidden(keys: str, expected: str) -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+b", *keys)
        assert selected(app) == [expected]


async def test_hjkl_reach_the_sidebar_again_once_it_is_shown() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+b", "ctrl+b", "l")
        assert selected(app) == ["verification-steps"]


async def test_hiding_with_a_sidebar_window_selected_selects_the_conversation() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("l", "ctrl+b")
        assert selected(app) == ["transcript"]
        assert app.focused is None


async def test_hiding_with_the_task_list_active_leaves_it_for_the_conversation() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("k", "l", "enter")
        assert app.focused is app.query_one(TaskList)
        await pilot.press("ctrl+b")
        assert not sidebar_shown(app)
        assert app.focused is None
        assert selected(app) == ["transcript"]


async def test_hiding_with_the_conversation_active_keeps_it_active() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("k", "enter")
        shown = app.query_one(Transcript).shown
        assert app.focused is shown
        await pilot.press("ctrl+b")
        assert not sidebar_shown(app)
        assert app.focused is shown
        assert selected(app) == ["transcript"]


async def test_ctrl_b_while_typing_leaves_the_sidebar_alone() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i", *"hi", "ctrl+b")
        assert sidebar_shown(app)
        assert app.focused is prompt


async def test_ctrl_b_does_nothing_over_a_modal_screen() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e", "ctrl+b")
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        assert sidebar_shown(app)


async def test_the_sidebar_keeps_updating_while_hidden() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+b")
        await feed_events(session, pilot, TasksChanged(tasks=(task("t1"),)))
        await pilot.press("ctrl+b")
        assert [state.id for state in app.query_one(TaskList).tasks] == ["t1"]


async def test_a_running_task_ticking_while_the_sidebar_is_hidden_shows_its_spinner_after() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        tasks = app.query_one(TaskList)
        tasks.clock = lambda: 0.0
        running = task("t1").model_copy(update={"status": "running"})
        await feed_events(session, pilot, TasksChanged(tasks=(running,)))
        await pilot.press("ctrl+b")
        await pilot.pause()
        tasks.clock = lambda: 1 / SPINNER_FPS
        tasks.tick()
        await pilot.press("ctrl+b")
        await pilot.pause()
        frame = spinner_frame(1 / SPINNER_FPS)
        assert frame != spinner_frame(0.0)
        assert tasks.render_line(0).text.strip().startswith(f"1. {frame} ")


def reason_line(option: Option) -> str:
    """The last line of a task row: its reason."""
    prompt = option.prompt
    assert isinstance(prompt, Text)
    return prompt.plain.splitlines()[-1]


async def test_a_reason_changed_while_the_sidebar_is_hidden_is_cut_to_the_shown_width() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+b")
        await pilot.pause()
        failed = task("t1").model_copy(update={"status": "error", "reason": "Tests failed " * 20})
        await feed_events(session, pilot, TasksChanged(tasks=(failed,)))
        await pilot.press("ctrl+b")
        await pilot.pause()
        tasks = app.query_one(TaskList)
        line = reason_line(tasks.get_option_at_index(0))
        assert line.endswith("…")
        assert len(line) == tasks.content_region.width - tasks.styles.scrollbar_size_vertical
