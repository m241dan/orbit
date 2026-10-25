"""The conversation's tabs, without Textual: the tabs agents open and close, the markers on them,
what each shows, and how tab, shift+tab and ctrl+t move between them."""

from orbit.protocol import (
    AGENT_FAILED,
    PLANNER,
    STARTUP_TASK,
    TIMED_OUT,
    AgentExited,
    AgentReply,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    ErrorOccurred,
    Event,
    SendMessage,
    StepState,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    TurnFinished,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from tests.frontend_fakes import Shell

PINNED = ["planner", "coder", "reviewer", "super-reviewer", "configurator"]
STRIP = ["All", *PINNED]
LINT_STEP = VerificationChanged(steps=(StepState(name="Lint"),))
NOTES_AFTER_HEADER = [
    "🔍 Verifying: Lint…",
    "❌ Verification failed: Lint",
    "✅ Verification passed: Lint",
    "⏩ Verification skipped: no files changed",
    "⇢ Merged into the base branch.",
]


def started(agent_id: str, role: str = "coder") -> AgentStarted:
    return AgentStarted(agent_id=agent_id, role=role, task_id="1")


def said(agent_id: str, text: str) -> AssistantMessage:
    return AssistantMessage(turn_id="t", text=text, agent_id=agent_id)


def exited(agent_id: str) -> AgentExited:
    return AgentExited(agent_id=agent_id, reason="finished")


def task(task_id: str, role: str = "coder") -> TaskState:
    return TaskState(id=task_id, task=TaskSpec(prompt=f"do {task_id}", role=role))


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


async def test_the_strip_starts_on_all_and_the_conversation_is_untitled() -> None:
    shell = Shell()
    assert (shell.tab_strip, shell.active_tab) == (STRIP, "All")
    assert shell.window.title == "Conversation"


async def test_pinned_role_agents_add_no_tabs_but_mark_theirs_at_work() -> None:
    shell = Shell()
    await shell.feed(*(started(f"agent-{i}", role) for i, role in enumerate(PINNED)))
    # Every role but the planner is at work, whose marker follows its busy lane.
    assert shell.tab_strip == ["All", "planner", *(f"● {role}" for role in PINNED[1:])]


async def test_each_agent_in_another_role_adds_a_tab_labelled_by_role_until_it_exits() -> None:
    shell = Shell()
    await shell.feed(started("agent-1", "checker"), started("agent-2", "linter"))
    await shell.feed(started("agent-3", "checker"))
    assert shell.tab_strip == [*STRIP, "checker", "linter", "checker 2"]
    assert shell.window.conversation.only is None
    await shell.feed(exited("agent-1"))
    assert shell.tab_strip == [*STRIP, "linter", "checker"]
    assert shell.window.title == "Conversation"


async def test_clicking_a_tab_shows_only_that_roles_conversation() -> None:
    shell = Shell()
    await shell.feed(
        AssistantMessage(turn_id="t", text="chat"),
        started("agent-1"),
        said("agent-1", "coding"),
        started("agent-2", "reviewer"),
        said("agent-2", "reviewing"),
    )
    await shell.click_tab("● reviewer *")
    assert shell.window.conversation.only == "reviewer"
    assert shell.window.title == "Conversation · ● reviewer"
    assert shell.shown() == ["▶ agent-2 (reviewer) started", "reviewing"]
    await shell.feed(said("agent-1", "more"))
    assert shell.shown() == ["▶ agent-2 (reviewer) started", "reviewing"]
    await shell.click_tab("All")
    assert shell.window.conversation.only is None
    assert shell.window.title == "Conversation"
    assert len(shell.shown()) == 6


async def test_an_exited_agents_tab_goes_and_its_entries_stay_under_all() -> None:
    shell = Shell()
    await shell.feed(started("agent-1", "checker"), said("agent-1", "coding"))
    await shell.feed(started("agent-2", "checker"))
    await shell.click_tab("checker *")
    assert shell.window.conversation.only == "agent-1"
    # Another agent leaving doesn't change what's shown.
    await shell.feed(exited("agent-2"))
    assert shell.tab_strip == [*STRIP, "checker"]
    assert shell.window.conversation.only == "agent-1"
    await shell.feed(exited("agent-1"))
    assert shell.tab_strip == STRIP
    assert shell.window.conversation.only is None
    assert shell.window.title == "Conversation"
    assert "coding" in shell.shown()


async def test_the_shown_tabs_title_follows_its_relabelling() -> None:
    shell = Shell()
    await shell.feed(started("agent-1", "checker"), started("agent-2", "checker"))
    await shell.click_tab("checker 2")
    assert shell.window.title == "Conversation · checker 2"
    await shell.feed(exited("agent-1"))
    assert shell.tab_strip == [*STRIP, "checker"]
    assert shell.window.conversation.only == "agent-2"
    assert shell.window.title == "Conversation · checker"


async def test_a_relabel_of_a_tab_no_longer_shown_leaves_the_title_alone() -> None:
    shell = Shell()
    shell.frontend.retitle("coder", "● coder")
    assert shell.window.title == "Conversation"


async def test_coder_output_lands_in_the_coder_tab_which_keeps_it_for_later_coders() -> None:
    shell = Shell()
    await shell.feed(started("agent-1"), said("agent-1", "coding"))
    assert shell.tab_strip == ["All", "planner", "● coder *", *PINNED[2:]]
    await shell.click_tab("● coder *")
    assert shell.window.conversation.only == "coder"
    assert shell.window.title == "Conversation · ● coder"
    await shell.feed(exited("agent-1"))
    assert shell.tab_strip == STRIP
    assert shell.window.title == "Conversation · coder"
    assert shell.window.conversation.only == "coder"
    await shell.feed(started("agent-2"), said("agent-2", "more"))
    assert shell.shown() == [
        "▶ agent-1 (coder) started",
        "coding",
        "agent-1 done · 0s",
        "▶ agent-2 (coder) started",
        "more",
    ]


async def test_concurrent_reviewers_share_the_reviewer_tab() -> None:
    shell = Shell()
    await shell.feed(
        started("agent-1", "reviewer"),
        started("agent-2", "reviewer"),
        started("agent-3", "super-reviewer"),
        ErrorOccurred(agent_id="agent-1", code=AGENT_FAILED, message="boom"),
        TurnFinished(turn_id="t", agent_id="agent-2", reason="cancelled"),
        ErrorOccurred(code=TIMED_OUT, message="lost"),
    )
    assert shell.tab_strip == [
        "All",
        "planner",
        "coder",
        "● reviewer *",
        "● super-reviewer",
        "configurator",
    ]
    await shell.click_tab("● reviewer *")
    assert shell.shown() == [
        "▶ agent-1 (reviewer) started",
        "▶ agent-2 (reviewer) started",
        "agent-1: boom",
        "agent-2 stopped",
    ]
    await shell.cycle(-1, 3)
    assert shell.active_tab == "All"
    # An error with no agent shows unprefixed, under All only.
    assert shell.shown()[-1] == "lost"


async def test_the_startup_checks_tab_goes_when_it_exits() -> None:
    shell = Shell()
    await shell.feed(started("startup-check", "checker"))
    assert shell.tab_strip == [*STRIP, "checker"]
    await shell.feed(exited("startup-check"))
    assert shell.tab_strip == STRIP


async def test_background_output_marks_a_tab_until_it_is_shown() -> None:
    shell = Shell()
    rest = PINNED[3:]
    await shell.feed(started("agent-1"), started("agent-2", "reviewer"))
    await shell.feed(said("agent-1", "coding"), said("agent-2", "hmm"))
    assert shell.tab_strip == ["All", "planner", "● coder *", "● reviewer *", *rest]
    await shell.feed(AssistantMessage(turn_id="t", text="chat"))
    assert shell.tab_strip == ["All", "planner", "● coder *", "● reviewer *", *rest]
    await shell.cycle(1, 2)
    assert shell.tab_strip == ["All", "planner", "● coder", "● reviewer *", *rest]
    assert shell.window.title == "Conversation · ● coder"
    # Output for the tab being shown doesn't mark it.
    await shell.feed(said("agent-1", "more"))
    assert shell.tab_strip == ["All", "planner", "● coder", "● reviewer *", *rest]
    await shell.cycle(-1)
    await shell.feed(said("agent-1", "again"))
    assert shell.tab_strip == ["All", "planner", "● coder *", "● reviewer *", *rest]


async def test_tab_and_shift_tab_cycle_the_tabs_and_wrap() -> None:
    shell = Shell()
    await shell.feed(started("agent-1", "checker"))
    for role in PINNED:
        await shell.cycle(1)
        assert (shell.active_tab, shell.window.conversation.only) == (role, role)
    await shell.cycle(1)
    assert (shell.active_tab, shell.window.conversation.only) == ("checker", "agent-1")
    await shell.cycle(1)
    assert (shell.active_tab, shell.window.conversation.only) == ("All", None)
    await shell.cycle(-1)
    assert shell.active_tab == "checker"
    await shell.cycle(-1)
    assert shell.active_tab == "configurator"
    await shell.cycle(-1, 5)
    assert shell.active_tab == "All"


async def test_tab_with_no_agents_cycles_all_and_the_pinned_tabs() -> None:
    shell = Shell()
    await shell.cycle(1)
    assert shell.active_tab == "planner"
    await shell.cycle(1, 5)
    assert shell.active_tab == "All"
    await shell.cycle(-1, 2)
    assert shell.active_tab == "super-reviewer"
    await shell.cycle(1, 2)
    assert shell.active_tab == "All"
    assert shell.window.conversation.only is None


async def test_planner_output_shows_under_planner_and_all_but_not_other_tabs() -> None:
    shell = Shell()
    # The user's planner prompt is on the planner tab before the planner starts.
    await shell.submit("plan")
    await shell.cycle(1)
    await shell.feed(
        started("agent-1", "planner"),
        said("agent-1", "drafting"),
        AgentReply(agent_id="agent-1", request_id="r", text="Added."),
        started("agent-2"),
        said("agent-2", "coding"),
    )
    # The planner gets no tab of its own; its tab is marked while it answers the prompt.
    assert shell.tab_strip == ["All", "● planner", "● coder *", *PINNED[2:]]
    assert shell.window.title == "Conversation · ● planner"
    planner = ["plan", "▶ agent-1 (planner) started", "drafting", "orbit → agent-1: Added."]
    assert shell.shown() == planner
    await shell.cycle(1)
    assert shell.shown() == ["▶ agent-2 (coder) started", "coding"]
    # Output while another tab is shown marks the planner tab.
    await shell.feed(said("agent-1", "more"))
    assert shell.tab_strip == ["All", "● planner *", "● coder", *PINNED[2:]]
    await shell.cycle(-1, 2)
    assert shell.active_tab == "All"
    assert shell.shown() == [*planner, "▶ agent-2 (coder) started", "coding", "more"]
    # The planner exiting leaves its tab, and only its busy lane clears its marker.
    await shell.feed(exited("agent-1"), exited("agent-2"))
    await shell.cycle(1)
    assert shell.tab_strip == ["All", "● planner", *PINNED[1:]]
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    assert shell.tab_strip == STRIP
    assert shell.shown()[-1] == "done · 0s"


async def test_planner_turn_rules_and_errors_show_under_planner_and_all() -> None:
    shell = Shell()
    await shell.feed(started("agent-1", "planner"), started("agent-2"))
    # Show the coder's tab, so the planner's entries mark the planner tab.
    await shell.cycle(1, 2)
    assert shell.active_tab == "● coder"
    await shell.feed(
        TurnFinished(turn_id="t", agent_id="agent-1", reason="cancelled"),
        ErrorOccurred(agent_id="agent-1", code=AGENT_FAILED, message="boom"),
    )
    assert shell.tab_strip == ["All", "planner *", "● coder", *PINNED[2:]]
    assert shell.shown() == ["▶ agent-2 (coder) started"]
    await shell.cycle(-1, 2)
    assert shell.active_tab == "All"
    # The planner's rule isn't headed by its id.
    entries = ["stopped", "agent-1: boom"]
    assert shell.shown()[-2:] == entries
    await shell.cycle(1)
    assert shell.active_tab == "planner"
    assert shell.shown() == ["▶ agent-1 (planner) started", *entries]


async def test_the_prompt_talks_to_the_planner_and_each_stop_stops_its_own_lane() -> None:
    shell = Shell()
    await shell.cycle(1)
    assert shell.marks.title == "Prompt · planner"
    await shell.submit("plan")
    await shell.accept_last()
    # Stopping the run works even on the planner tab; the planner has its own stop.
    await shell.frontend.cancel_turn()
    await shell.frontend.stop_planner()
    await shell.cycle(-1)
    assert shell.marks.title == "Prompt · planner · busy"
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    await shell.submit("hi")
    await shell.frontend.cancel_turn()
    await shell.frontend.stop_planner()
    assert shell.wire.commands == [
        SendMessage(agent=PLANNER, text="plan"),
        CancelTurn(lane="work"),
        CancelTurn(lane="planner"),
        SendMessage(agent=PLANNER, text="hi"),
        CancelTurn(lane="work"),
        CancelTurn(lane="planner"),
    ]
    await shell.cycle(1)
    assert shell.shown() == ["plan", "hi"]


async def test_the_prompt_sends_while_a_run_is_busy() -> None:
    shell = Shell()
    await shell.feed(BusyChanged(busy=True))
    assert shell.marks.title == "Prompt · planner"
    await shell.submit("plan")
    assert shell.marks.title == "Prompt · planner · busy"  # Until the planner answers.
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    assert shell.marks.title == "Prompt · planner"
    # The work lane going idle doesn't free the prompt while the planner answers.
    await shell.submit("more")
    await shell.feed(BusyChanged(busy=False, lane="work"))
    assert shell.marks.title == "Prompt · planner · busy"


async def test_ctrl_t_jumps_to_the_planner_tab_and_back() -> None:
    shell = Shell()
    await shell.feed(started("agent-1"), started("agent-2", "checker"))
    await shell.cycle_conversations()
    assert shell.active_tab == "planner"
    assert shell.window.title == "Conversation · planner"
    await shell.cycle_conversations()
    assert shell.active_tab == "All"
    await shell.cycle(1, 3)
    assert shell.active_tab == "reviewer"
    await shell.cycle_conversations()
    assert shell.active_tab == "planner"
    await shell.cycle_conversations()
    assert shell.active_tab == "reviewer"
    # Back to All when the tab it came from has gone.
    await shell.cycle(-1, 4)
    assert shell.active_tab == "checker"
    await shell.cycle_conversations()
    await shell.feed(exited("agent-2"))
    await shell.cycle_conversations()
    assert shell.active_tab == "All"
    assert shell.wire.commands == []


async def test_a_roles_tab_is_marked_while_any_of_its_agents_runs() -> None:
    shell = Shell()
    await shell.feed(started("agent-1"), started("agent-2"))
    assert shell.tab_strip == ["All", "planner", "● coder", *PINNED[2:]]
    await shell.feed(exited("agent-1"))
    assert shell.tab_strip == ["All", "planner", "● coder", *PINNED[2:]]
    await shell.feed(exited("agent-2"))
    assert shell.tab_strip == STRIP


async def test_the_planner_tab_is_marked_while_it_answers() -> None:
    shell = Shell()
    await shell.cycle_conversations()
    await shell.submit("hi")
    # Marked as soon as the prompt is sent, before the backend reports the planner busy.
    assert shell.tab_strip == ["All", "● planner", *PINNED[1:]]
    assert shell.window.title == "Conversation · ● planner"
    await shell.feed(BusyChanged(busy=True, lane="planner"))
    assert shell.tab_strip == ["All", "● planner", *PINNED[1:]]
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    assert shell.tab_strip == STRIP
    assert shell.window.title == "Conversation · planner"
    # The work lane is the task run's, not the planner's.
    await shell.feed(BusyChanged(busy=True, lane="work"))
    assert shell.tab_strip == STRIP


async def test_a_coder_tasks_notes_show_under_coder_and_all_and_mark_coder_unread() -> None:
    shell = Shell()
    await shell.feed(TasksChanged(tasks=(task("1"),)), LINT_STEP)
    await shell.cycle(1)
    assert shell.active_tab == "planner"
    await shell.feed(*task_notes("1"))
    # Notes landing in a tab not being shown mark it, like agent output does.
    assert shell.tab_strip == ["All", "planner", "coder *", *PINNED[2:]]
    assert shell.shown() == []
    notes = ["Task 1: do 1", *NOTES_AFTER_HEADER]
    await shell.cycle(1)
    assert shell.active_tab == "coder"
    assert shell.tab_strip == STRIP
    assert shell.shown() == notes
    # Notes for the tab being shown don't mark it.
    await shell.feed(TaskMerged(task_id="1", result="conflict", detail="x"))
    assert shell.tab_strip == STRIP
    assert shell.shown() == [*notes, "⚠ Not merged (conflict): x"]
    await shell.cycle(-1, 2)
    assert shell.active_tab == "All"
    assert shell.shown() == [*notes, "⚠ Not merged (conflict): x"]


async def test_a_reviewer_tasks_notes_show_under_reviewer_not_coder() -> None:
    shell = Shell()
    await shell.feed(TasksChanged(tasks=(task("1"), task("2", "reviewer"))), LINT_STEP)
    await shell.feed(*task_notes("2"))
    assert shell.tab_strip == ["All", "planner", "coder", "reviewer *", *PINNED[3:]]
    await shell.cycle(1, 2)
    assert shell.active_tab == "coder"
    assert shell.shown() == []
    await shell.cycle(1)
    assert shell.active_tab == "reviewer"
    assert shell.shown() == ["Task 2: do 2", *NOTES_AFTER_HEADER]


async def test_notes_on_no_listed_pinned_task_stay_under_all() -> None:
    shell = Shell()
    await shell.feed(TasksChanged(tasks=(task("1", "checker"),)), LINT_STEP)
    await shell.cycle(1)
    assert shell.active_tab == "planner"
    await shell.feed(
        # The startup check verifies the base commit, not a task of the list.
        VerificationStarted(task_id=STARTUP_TASK, step=0),
        VerificationFinished(task_id=STARTUP_TASK, step=0, passed=True),
        # A task removed meanwhile, or never listed.
        *task_notes("9"),
        # A task in a role with no pinned tab.
        *task_notes("1"),
    )
    assert shell.tab_strip == STRIP
    assert shell.shown() == []
    for _ in PINNED[1:]:
        await shell.cycle(1)
        assert shell.shown() == []
    await shell.cycle(1)
    assert shell.active_tab == "All"
    assert shell.shown() == [
        "🔍 Verifying: Lint…",
        "✅ Verification passed: Lint",
        "Task 9",
        *NOTES_AFTER_HEADER,
        "Task 1: do 1",
        *NOTES_AFTER_HEADER,
    ]
