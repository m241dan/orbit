import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
from textual import events
from textual.pilot import Pilot
from textual.widgets import Footer, Tab
from textual.worker import WorkerFailed

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    Event,
    EventEnvelope,
    Notice,
    SendMessage,
    Settings,
    StepState,
    TaskMerged,
    TasksChanged,
    TaskStarted,
    UpdateSettings,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui import OrbitApp
from orbit.tui.event_stream import event_batches
from orbit.tui.tabs import ConversationTabs
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.transcript_log import TranscriptLog
from orbit.tui.view.common import spinner_frame
from orbit.tui.view.events import coalesce
from orbit.tui.view.frontend import Frontend
from orbit.tui.widgets import ChatPrompt, PickerScreen, SidePanel
from tests.tui_fakes import (
    STRIP,
    FakeSession,
    accept_last,
    active_tab,
    call,
    feed_events,
    picker,
    result,
    said,
    scrolled_to_end,
    selected,
    show_settings,
    shown,
    started,
    tab_strip,
    task,
)


async def test_quitting_closes_the_session() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+c")
    assert session.closed


async def test_ctrl_s_stops_the_run_and_ctrl_x_the_planner() -> None:
    session = FakeSession()
    async with OrbitApp(session).run_test() as pilot:
        await pilot.press("ctrl+s", "ctrl+x")
        assert session.commands == [CancelTurn(lane="work"), CancelTurn(lane="planner")]


async def test_a_failed_event_stream_shows_an_error_line() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, Notice(message="before"))
        await session.queue.put(RuntimeError("the queue broke"))
        await pilot.pause()
        assert shown(app)[-1] == "The backend's event stream failed: the queue broke"


async def test_a_failed_hello_shows_an_error_line() -> None:
    session = FakeSession()
    session.hello_error = RuntimeError("no git")
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running
        assert shown(app)[-1] == "The backend failed to start the session: no git"


async def test_the_prompt_takes_typing_while_the_planner_is_busy() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, BusyChanged(busy=True, lane="planner"))
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i", *"hi")
        assert app.focused is prompt
        assert prompt.text == "hi"


async def test_enter_while_the_planner_is_busy_keeps_the_draft_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    app = OrbitApp(session)
    notes: list[tuple[str, object]] = []
    monkeypatch.setattr(app, "notify", lambda text, **kw: notes.append((text, kw["severity"])))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, BusyChanged(busy=True, lane="planner"))
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i", *"hi", "left", "enter")
        assert session.commands == []
        assert prompt.text == "hi"
        assert prompt.cursor_location == (0, 1)
        assert app.focused is prompt
        assert notes == [("Planner is busy", "warning")]


async def test_a_pasted_multi_line_prompt_is_sent_whole_and_the_box_shrinks() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 30)) as pilot:
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i")
        app.post_message(events.Paste("  first\nsecond\n"))
        await pilot.pause()
        assert prompt.region.height == 5  # three rows, the last empty, inside a border
        await pilot.press("enter")
        await pilot.pause()
        assert session.commands == [SendMessage(agent=PLANNER, text="first\nsecond")]
        await accept_last(session, pilot)
        assert prompt.text == ""
        assert prompt.region.height == 3


async def test_layout_stacks_display_over_prompt_over_footer() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(80, 24)):
        tabs = app.query_one(ConversationTabs)
        transcript = app.query_one(Transcript)
        prompt = app.query_one(ChatPrompt)
        footer = app.query_one(Footer)

        assert tabs.region.y == 0
        assert tabs.region.bottom == transcript.region.y
        assert tabs.region.width == transcript.region.width
        assert transcript.region.bottom == prompt.region.y
        assert prompt.region.bottom == footer.region.y
        assert footer.region.bottom == 24
        # The side panels take 33% (26 columns); conversation and prompt get the rest.
        assert transcript.region.width == prompt.region.width == 54
        assert prompt.region.height == 3  # one line of text inside a border


async def test_display_absorbs_resize_and_prompt_stays_at_bottom() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(80, 24)) as pilot:
        transcript = app.query_one(Transcript)
        prompt = app.query_one(ChatPrompt)
        footer = app.query_one(Footer)
        before = transcript.region.height

        await pilot.resize_terminal(100, 40)
        await pilot.pause()

        assert transcript.region.height == before + 16
        assert prompt.region.height == 3
        assert prompt.region.bottom == footer.region.y


async def test_sections_are_labelled() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert app.query_one(Transcript).border_title == "Conversation"
        assert app.query_one(ChatPrompt).border_title == "Prompt · planner"
        assert "planner" in str(app.query_one(ChatPrompt).placeholder)


async def test_prompt_starts_in_normal_mode_and_ignores_typing_and_clicks() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(ChatPrompt)
        assert app.focused is None
        await pilot.press(*"hello")
        await pilot.click("#prompt")
        assert app.focused is None
        assert prompt.text == ""


async def test_i_enters_insert_mode_without_typing_the_i() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i")
        assert app.focused is prompt
        await pilot.press(*"hi")
        assert prompt.text == "hi"


async def test_escape_in_insert_mode_returns_to_normal_mode_without_cancelling() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i", "a", "escape")
        assert app.focused is None
        assert session.commands == []

        # Back in normal mode: keys don't type, the draft is kept, ctrl+s cancels.
        await pilot.press("b", "ctrl+s")
        assert prompt.text == "a"
        assert session.commands == [CancelTurn()]

        await pilot.press("i", "b")
        assert prompt.text == "ab"


async def test_display_follows_latest_output() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(80, 24)) as pilot:
        for i in range(40):
            await session.queue.put(AssistantMessage(turn_id="t", text=f"line {i}"))
        await pilot.pause()

        log = app.query_one("Transcript > TranscriptLog.-shown", TranscriptLog)
        assert log.max_scroll_y > 0
        assert await scrolled_to_end(pilot, log)


async def test_pickers_are_built_once_and_reused() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        opened: list[object] = []
        for key in ["ctrl+o", "ctrl+o", "ctrl+l", "ctrl+l"]:
            await pilot.press(key)
            opened.append(app.screen)
            await pilot.press("escape")

        assert opened[0] is opened[1]
        assert opened[2] is opened[3]
        # A later settings report with the same choices keeps the cached picker.
        await show_settings(session, pilot, Settings(model="opus"))
        await pilot.press("ctrl+o")
        assert app.screen is opened[0]
        assert picker(app)[2] == 1


async def test_picker_keys_ignored_while_a_picker_is_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings())
        await pilot.press("ctrl+o")
        model_picker = app.screen
        await pilot.press("ctrl+o", "ctrl+l")
        assert app.screen is model_picker
        assert len(app.screen_stack) == 2


async def test_ctrl_c_quits_even_with_prompt_focused() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("i")
        assert app.focused is app.query_one(ChatPrompt)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running


async def test_ctrl_q_does_not_quit() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running


async def test_side_panels_fill_right_third_top_and_bottom() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(120, 41)) as pilot:
        await pilot.pause()
        tasks = app.query_one("#task-list")
        steps = app.query_one("#verification-steps", SidePanel)
        transcript = app.query_one(Transcript).region
        prompt = app.query_one(ChatPrompt).region
        assert (tasks.border_title, steps.border_title) == ("Task list", "Verification steps")

        agents = app.query_one("#agents")
        # 33% of the width (39.6 rounds down): the task list and steps share the height above
        # the agents pane, which sits at the bottom, above the footer.
        for panel in (tasks, steps, agents):
            assert panel.region.width == 39
            assert panel.region.right == 120
        assert tasks.region.y == 0
        assert steps.region.y == tasks.region.bottom
        assert agents.region.y == steps.region.bottom
        assert agents.region.bottom == 40
        assert abs(tasks.region.height - steps.region.height) <= 1  # An odd row goes to one.

        # Conversation and prompt take the rest of the width on the left.
        assert transcript.width == prompt.width == 81
        assert prompt.bottom == 40


async def test_prompt_window_is_selected_on_start_and_highlighted() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test():
        assert selected(app) == ["prompt"]
        assert app.focused is None
        tasks = app.query_one("#task-list")
        unselected_border = tasks.styles.border_top
        tasks.add_class("selected")
        assert tasks.styles.border_top != unselected_border


async def test_border_highlight_follows_selection() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        tasks = app.query_one("#task-list")
        before = tasks.styles.border_top
        await pilot.press("k", "l")
        assert tasks.styles.border_top != before
        await pilot.press("h")
        assert tasks.styles.border_top == before


async def test_the_conversations_border_follows_selection_and_its_logs_keep_theirs() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        transcript = app.query_one(Transcript)
        log = transcript.shown
        before, log_border = transcript.styles.border_top, log.styles.border_top
        await pilot.press("k")
        assert transcript.styles.border_top != before
        assert log.styles.border_top == log_border
        await pilot.press("j")
        assert transcript.styles.border_top == before


async def test_enter_does_nothing_on_a_window_without_a_list() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        await pilot.press("l", "enter")
        assert app.focused is None
        assert selected(app) == ["verification-steps"]


async def test_i_on_prompt_enters_insert_mode_and_hjkl_type() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        prompt = app.query_one(ChatPrompt)
        normal_border = prompt.styles.border_top
        await pilot.press("k", "j", "i")
        assert app.focused is prompt
        assert prompt.styles.border_top != normal_border  # active, not just selected
        await pilot.press(*"hjkl")
        assert prompt.text == "hjkl"
        assert selected(app) == ["prompt"]

        await pilot.press("escape", "k")
        assert app.focused is None
        assert selected(app) == ["transcript"]


async def test_pickers_move_with_j_and_k() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await show_settings(session, pilot, Settings(model="opus", tool_access="off"))
        await pilot.press("ctrl+o", "j")
        assert picker(app)[2] == 2
        await pilot.press("k", "k")
        assert picker(app)[2] == 0
        await pilot.press("escape")
        # The window selection underneath didn't move.
        assert selected(app) == ["prompt"]

        await pilot.press("ctrl+l", "j", "j", "k", "enter")
        assert session.commands == [
            UpdateSettings(settings=Settings(model="opus", tool_access="restricted"))
        ]


def test_spinner_cycles_the_arc_eight_frames_a_second() -> None:
    frames = [spinner_frame(i / 8) for i in range(7)]
    # Padded to two columns, like the emoji it stands beside.
    assert frames == ["◜ ", "◠ ", "◝ ", "◞ ", "◡ ", "◟ ", "◜ "]


async def test_the_tab_strip_starts_with_all_and_the_pinned_roles_and_never_takes_focus() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test() as pilot:
        tabs = app.query_one(ConversationTabs)
        assert tab_strip(app) == STRIP
        assert tabs.active_tab is not None and tabs.active_tab.label_text == "All"
        assert not tabs.focusable
        assert app.query_one(Transcript).border_title == "Conversation"
        await pilot.click(Tab)
        assert app.focused is None
        # h/j/k/l still move straight between the conversation and the prompt.
        await pilot.press("k")
        assert app.query_one(Transcript).has_class("selected")
        await pilot.press("j")
        assert app.query_one(ChatPrompt).has_class("selected")


async def inspect_latest(app: OrbitApp, pilot: Pilot[None]) -> Transcript:
    """Open the shown tab's list and the detail pane on its latest entry."""
    await pilot.press("k", "enter", "enter")
    await pilot.pause()
    transcript = app.query_one(Transcript)
    assert transcript.detail.opened and app.focused is transcript.shown
    return transcript


async def test_relabelling_the_shown_planner_tab_keeps_its_list_and_details() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, call(PLANNER, "c1"), result(PLANNER, "c1", "x"))
        await pilot.press("ctrl+t")
        transcript = await inspect_latest(app, pilot)

        await feed_events(session, pilot, BusyChanged(busy=True, lane="planner"))
        assert transcript.border_title == "Conversation · ● planner"
        assert app.focused is transcript.shown and transcript.detail.opened

        await pilot.press("tab")  # The pane itself.
        await feed_events(session, pilot, BusyChanged(busy=False, lane="planner"))
        assert transcript.border_title == "Conversation · planner"
        assert transcript.detail.has_focus and transcript.detail.opened
        assert selected(app) == ["detail"]


async def click_rows(pilot: Pilot[None], log: TranscriptLog) -> None:
    """Click the first line of each of ``log``'s rows."""
    for top, _ in log.row_lines():
        await pilot.click(log, offset=(4, top))
    await pilot.pause()


async def test_a_click_on_an_inactive_log_neither_toggles_nor_inspects() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session,
            pilot,
            said(PLANNER, "one\n\ntwo"),
            call(PLANNER, "c1"),
            result(PLANNER, "c1", "x"),
        )
        await pilot.press("ctrl+t")
        transcript = app.query_one(Transcript)
        log = transcript.shown
        before = [log.collapsed(row) for row in range(log.option_count)]
        await click_rows(pilot, log)
        assert [log.collapsed(row) for row in range(log.option_count)] == before
        assert not log.inspecting and not transcript.detail.opened
        assert app.focused is None


async def test_a_click_on_the_active_log_inspects_a_tool_call() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, call(PLANNER, "c1"), result(PLANNER, "c1", "x"))
        await pilot.press("ctrl+t", "k", "enter")
        transcript = app.query_one(Transcript)
        await click_rows(pilot, transcript.shown)
        assert transcript.shown.inspecting and transcript.detail.opened


async def test_relabelling_the_shown_agent_tab_keeps_its_list_and_details() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(
            session, pilot, started("agent-1"), call("agent-1", "c1"), result("agent-1", "c1", "x")
        )
        await pilot.click(list(app.query_one(ConversationTabs).query(Tab))[2])
        transcript = await inspect_latest(app, pilot)
        await pilot.press("tab")

        await feed_events(session, pilot, AgentExited(agent_id="agent-1", reason="finished"))
        assert transcript.border_title == "Conversation · coder"
        assert transcript.detail.has_focus and transcript.detail.opened
        assert selected(app) == ["detail"]


async def test_f_no_longer_filters() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "coding"))
        await pilot.press("f")
        assert app.query_one(Transcript).only is None
        assert len(shown(app)) == 2


async def test_tab_shows_in_the_footer() -> None:
    app = OrbitApp(FakeSession())
    async with app.run_test(size=(200, 30)):
        keys = {b.binding.description for b in app.active_bindings.values() if b.binding.show}
        assert "Next tab" in keys


async def test_tab_keeps_its_normal_behaviour_outside_normal_mode() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"))

        # Typing in the prompt.
        await pilot.press("i", "tab")
        assert active_tab(app) == "All"
        assert app.focused is not None
        await pilot.press("escape")
        assert app.focused is None

        # The task list in list mode, then its editor, where j/k still move between fields.
        await pilot.press("l", "k", "enter")
        assert app.focused is app.query_one(TaskList)
        await pilot.press("shift+tab")
        assert active_tab(app) == "All"
        app.set_focus(None)
        await pilot.press("l", "k", "enter", "enter")
        assert len(app.screen_stack) == 2
        await pilot.press("tab", "shift+tab", "j")
        assert active_tab(app) == "All"
        assert app.screen.query_one("#task-prompt").has_class("selected")
        await pilot.press("i", "tab")
        assert active_tab(app) == "All"


def held(app: OrbitApp) -> int | None:
    """The cursor the shown conversation log holds."""
    return app.query_one(Transcript).shown.highlighted


async def test_escape_keeps_its_behaviour_while_the_conversation_holds_a_cursor() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await show_settings(session, pilot, Settings())
        await feed_events(session, pilot, started("agent-1"), said("agent-1", "hi"))
        await pilot.press("k", "enter", "escape")
        assert held(app) == 1

        await pilot.press("ctrl+o")
        assert isinstance(app.screen, PickerScreen)
        await pilot.press("escape")  # The picker closes.
        assert len(app.screen_stack) == 1 and held(app) == 1
        await pilot.press("l", "k", "enter", "escape")  # The task list leaves list mode.
        assert app.focused is None and held(app) == 1
        await pilot.press("enter", "enter")
        assert len(app.screen_stack) == 2
        await pilot.press("escape")  # The task editor closes, back to the task list.
        assert len(app.screen_stack) == 1 and held(app) == 1
        await pilot.press("escape", "i", "escape")  # The prompt stops typing.
        assert app.focused is None and held(app) == 1

        await pilot.press("k", "escape")  # Back on the conversation, escape lets go.
        assert held(app) is None


async def test_ctrl_t_while_typing_keeps_the_prompt_and_its_text() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test(size=(120, 30)) as pilot:
        await feed_events(session, pilot, started("agent-1"))
        prompt = app.query_one(ChatPrompt)
        await pilot.press("i", *"hello")
        await pilot.press("ctrl+t")
        assert active_tab(app) == "planner"
        assert app.focused is prompt
        assert prompt.text == "hello"
        await pilot.press("ctrl+t")
        assert active_tab(app) == "All"
        assert app.focused is prompt
        assert prompt.text == "hello"
        assert session.commands == []


LINT_STEP = VerificationChanged(steps=(StepState(name="Lint"),))


def task_notes(task_id: str) -> list[Event]:
    """A task's whole run: its header, each kind of verification note, and its merge note."""
    return [
        TaskStarted(task_id=task_id),
        VerificationStarted(task_id=task_id, step=0),
        VerificationFinished(task_id=task_id, step=0, passed=False),
        VerificationFinished(task_id=task_id, step=0, passed=True),
        VerificationSkipped(task_id=task_id, reason="no_changes"),
        TaskMerged(task_id=task_id, result="merged"),
    ]


NOTES_AFTER_HEADER = [
    "🔍 Verifying: Lint…",
    "❌ Verification failed: Lint",
    "✅ Verification passed: Lint",
    "⏩ Verification skipped: no files changed",
    "⇢ Merged into the base branch.",
]


async def test_a_failed_render_cancels_the_event_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Holding the batches keeps garbage collection from closing them, as it would by chance.
    held: list[AsyncGenerator[list[Event]]] = []

    def holding(events: AsyncIterator[EventEnvelope]) -> AsyncGenerator[list[Event]]:
        held.append(event_batches(events))
        return held[-1]

    def fails(frontend: Frontend, event: Event) -> None:
        raise RuntimeError("render failed")

    monkeypatch.setattr("orbit.tui.app.event_batches", holding)
    monkeypatch.setattr(Frontend, "render", fails)
    session = FakeSession()
    await session.queue.put(Notice(message="1"))
    with pytest.raises(WorkerFailed, match="render failed"):
        async with OrbitApp(session).run_test() as pilot:
            await pilot.pause()
    await session.queue.put(Notice(message="2"))
    await asyncio.sleep(0)
    # The reader is gone, so nothing takes the event.
    assert session.queue.qsize() == 1
    assert len(held) == 1


class BatchRecordingApp(OrbitApp):
    """Records the events rendered inside each outermost batch update, once :meth:`record`
    has it record what the frontend renders."""

    def __init__(self, session: FakeSession) -> None:
        super().__init__(session)
        self.batches: list[list[Event]] = []
        self._depth = 0

    @contextmanager
    def batch_update(self) -> Iterator[None]:
        if not self._depth:
            self.batches.append([])
        self._depth += 1
        try:
            with super().batch_update():
                yield
        finally:
            self._depth -= 1

    def record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Note each event the frontend renders in the batch it's rendered in."""
        render = Frontend.render

        def recording(frontend: Frontend, event: Event) -> None:
            self.batches[-1].append(event)
            render(frontend, event)

        monkeypatch.setattr(Frontend, "render", recording)


BURST: list[Event] = [
    TasksChanged(tasks=(task("1"),)),
    TasksChanged(tasks=(task("1"), task("2", "reviewer"))),
    LINT_STEP,
    TaskStarted(task_id="2"),
    started("agent-1", "reviewer"),
    said("agent-1", "looking"),
    TasksChanged(tasks=(task("2", "reviewer"),)),
    TasksChanged(tasks=(task("2", "reviewer"), task("3"))),
    *task_notes("2"),
]


async def test_a_burst_renders_in_one_batch_without_superseded_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    for event in BURST:
        session.queue.put_nowait(event)
    app = BatchRecordingApp(session)
    app.record(monkeypatch)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert [batch for batch in app.batches if batch] == [coalesce(BURST)]
        assert len(coalesce(BURST)) == len(BURST) - 2
