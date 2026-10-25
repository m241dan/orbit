from collections.abc import Mapping
from dataclasses import replace

from orbit.protocol import (
    INTERNAL,
    PLANNER,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
    Event,
    Notice,
    ProgressNoted,
    RunningAgent,
    TaskGaveUp,
    TaskSpec,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
)
from orbit.tui.view.tabs import (
    ALL_TAB_LABEL,
    PINNED_TABS,
    ROLE_TABS,
    ConversationTab,
    follow_running,
    mark_lane_busy,
    mark_tab_read,
    mark_tab_unread,
    pin_conversations,
    pinned_roles,
    replay_tabs,
    reset_tabs,
    running_roles,
    stopped_callers,
    tab_key,
    tab_labels,
    task_tab_key,
    track_roles,
    track_tabs,
)


def test_each_started_agent_in_another_role_gets_a_tab_until_it_exits() -> None:
    tabs: tuple[ConversationTab, ...] = ()
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-1", role="checker", task_id="1"))
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-2", role="linter", task_id="2"))
    assert tabs == (ConversationTab("agent-1", "checker"), ConversationTab("agent-2", "linter"))
    tabs = track_tabs(tabs, AgentExited(agent_id="agent-1", reason="finished"))
    assert tabs == (ConversationTab("agent-2", "linter"),)
    assert track_tabs(tabs, Notice(message="x")) is tabs


def test_tabs_are_named_by_role_and_numbered_among_those_open() -> None:
    assert ALL_TAB_LABEL == "All"
    assert tab_labels(()) == []
    tabs: tuple[ConversationTab, ...] = (
        ConversationTab("agent-1", "coder"),
        ConversationTab("agent-2", "reviewer"),
        ConversationTab("agent-3", "coder"),
        ConversationTab("agent-4", "coder"),
    )
    assert tab_labels(tabs) == ["coder", "reviewer", "coder 2", "coder 3"]
    tabs = track_tabs(tabs, AgentExited(agent_id="agent-1", reason="finished"))
    assert tab_labels(tabs) == ["reviewer", "coder", "coder 2"]


def test_output_from_a_background_agent_marks_its_tab_unread() -> None:
    tabs = (ConversationTab("agent-1", "coder"), ConversationTab("agent-2", "coder"))
    outputs: list[Event] = [
        AssistantMessage(turn_id="t", text="hi", agent_id="agent-2"),
        ToolCallStarted(turn_id="t", call_id="c", name="Read", arguments={}, agent_id="agent-2"),
        ToolCallFinished(turn_id="t", call_id="c", output="ok", agent_id="agent-2"),
        AgentRequest(agent_id="agent-2", request_id="r", tool="get_task", arguments={}),
        AgentReply(agent_id="agent-2", request_id="r", text="ok"),
        ProgressNoted(agent_id="agent-2", task_id="1", note="n"),
        TaskGaveUp(agent_id="agent-2", task_id="1", reason="r"),
        ErrorOccurred(agent_id="agent-2", code=INTERNAL, message="boom"),
        TurnFinished(turn_id="t", agent_id="agent-2", reason="cancelled"),
    ]
    for event in outputs:
        marked = track_tabs(tabs, event, active="agent-1")
        assert marked == (tabs[0], ConversationTab("agent-2", "coder", True))
        assert tab_labels(marked) == ["coder", "coder 2 *"]
        # Output for the tab being looked at isn't unread.
        assert track_tabs(tabs, event, active="agent-2") == tabs


def test_replayed_events_open_and_close_tabs_but_mark_none_unread() -> None:
    roles = {"agent-2": "reviewer"}
    tabs = replay_tabs(PINNED_TABS, AgentStarted(agent_id="agent-1", role="checker", task_id="1"))
    assert tabs == (*PINNED_TABS, ConversationTab("agent-1", "checker"))
    said = AssistantMessage(turn_id="t", text="hi", agent_id="agent-1")
    assert replay_tabs(tabs, said) is tabs
    reviewed = TurnFinished(turn_id="t", agent_id="agent-2", reason="completed")
    assert replay_tabs(tabs, reviewed, roles) is tabs
    assert replay_tabs(tabs, AgentExited(agent_id="agent-1", reason="finished")) == PINNED_TABS


def test_events_with_no_agent_mark_nothing() -> None:
    tabs = (ConversationTab("agent-1", "coder"),)
    assert track_tabs(tabs, AssistantMessage(turn_id="t", text="hi")) is tabs
    assert track_tabs(tabs, Notice(message="x"), active=None) is tabs


def test_a_turn_ending_marks_its_tab_for_its_rule() -> None:
    tabs = (ConversationTab("agent-1", "coder"),)
    event = TurnFinished(turn_id="t", agent_id="agent-1", reason="completed")
    assert track_tabs(tabs, event) == (ConversationTab("agent-1", "coder", True),)


def test_marking_a_tab_read_clears_only_its_marker() -> None:
    tabs = (ConversationTab("agent-1", "coder", True), ConversationTab("agent-2", "coder", True))
    assert mark_tab_read(tabs, "agent-1") == (
        ConversationTab("agent-1", "coder"),
        ConversationTab("agent-2", "coder", True),
    )
    assert tab_labels(mark_tab_read(tabs, "agent-1")) == ["coder", "coder 2 *"]


def test_the_pinned_tabs_are_the_built_in_roles_in_order() -> None:
    assert ROLE_TABS == (
        "planner",
        "experimenter",
        "coder",
        "reviewer",
        "super-reviewer",
        "configurator",
    )
    assert PLANNER == "planner"
    assert [tab.key for tab in PINNED_TABS] == list(ROLE_TABS)
    assert pinned_roles(PINNED_TABS) == frozenset(ROLE_TABS)
    assert [tab.lane for tab in PINNED_TABS] == ["planner", None, None, None, None, None]
    assert tab_labels(PINNED_TABS) == [
        "planner",
        "experimenter",
        "coder",
        "reviewer",
        "super-reviewer",
        "configurator",
    ]


def test_agents_in_a_pinned_role_get_no_tab_of_their_own() -> None:
    for index, role in enumerate(ROLE_TABS):
        started = AgentStarted(agent_id=f"agent-{index}", role=role, task_id="1")
        assert [tab.key for tab in track_tabs(PINNED_TABS, started)] == list(ROLE_TABS)


def test_a_pinned_roles_agent_exiting_leaves_its_tab() -> None:
    started = AgentStarted(agent_id="agent-1", role="coder", task_id="1")
    roles = track_roles({}, started)
    exited = AgentExited(agent_id="agent-1", reason="finished")
    assert track_roles(roles, exited) is roles
    assert track_tabs(track_tabs(PINNED_TABS, started), exited, roles=roles) == PINNED_TABS


def test_an_agent_named_like_a_pinned_role_doesnt_close_its_tab() -> None:
    exited = AgentExited(agent_id="coder", reason="finished")
    assert track_tabs(PINNED_TABS, exited) == PINNED_TABS


def test_each_pinned_roles_agents_are_keyed_to_its_tab() -> None:
    roles: dict[str, str] = {}
    for index, role in enumerate(ROLE_TABS):
        started = AgentStarted(agent_id=f"agent-{index}", role=role, task_id="1")
        roles = dict(track_roles(roles, started))
    assert [tab_key(f"agent-{index}", roles) for index in range(len(ROLE_TABS))] == list(ROLE_TABS)
    assert tab_key("other", roles) == "other"
    assert tab_key(None, roles) is None


def test_concurrent_reviewers_share_a_tab_and_the_super_reviewer_has_its_own() -> None:
    roles: Mapping[str, str] = {}
    for agent, role in (("a1", "reviewer"), ("a2", "reviewer"), ("a3", "super-reviewer")):
        roles = track_roles(roles, AgentStarted(agent_id=agent, role=role, task_id="1"))
    assert tab_key("a1", roles) == tab_key("a2", roles) == "reviewer"
    assert tab_key("a3", roles) == "super-reviewer"


def test_other_roles_are_not_tracked() -> None:
    roles: Mapping[str, str] = {}
    checker = AgentStarted(agent_id="startup-check", role="checker", task_id="")
    assert track_roles(roles, checker) is roles
    assert track_roles(roles, Notice(message="x")) is roles


def test_a_checker_gets_a_temporary_tab_after_the_pinned_ones() -> None:
    checker = AgentStarted(agent_id="startup-check", role="checker", task_id="")
    tabs = track_tabs(PINNED_TABS, checker)
    assert tabs == (*PINNED_TABS, ConversationTab("startup-check", "checker"))
    assert tab_labels(tabs)[-1] == "checker"
    exited = AgentExited(agent_id="startup-check", reason="finished")
    assert track_tabs(tabs, exited) == PINNED_TABS


def test_pinned_role_output_marks_its_tab_unless_shown() -> None:
    roles = {"agent-1": "reviewer", "agent-2": "reviewer"}
    said = AssistantMessage(turn_id="t", text="hi", agent_id="agent-2")
    marked = track_tabs(PINNED_TABS, said, active=None, roles=roles)
    assert tab_labels(marked) == [
        "planner",
        "experimenter",
        "coder",
        "reviewer *",
        "super-reviewer",
        "configurator",
    ]
    assert track_tabs(PINNED_TABS, said, active="reviewer", roles=roles) == PINNED_TABS
    assert mark_tab_read(marked, "reviewer") == PINNED_TABS


def _pinned(agent: str, role: str) -> tuple[AgentStarted, AgentExited]:
    return (
        AgentStarted(agent_id=agent, role=role, task_id="1"),
        AgentExited(agent_id=agent, reason="finished"),
    )


def test_a_pinned_roles_tab_is_marked_running_from_its_agents_start_to_its_exit() -> None:
    started, exited = _pinned("agent-1", "coder")
    roles = track_roles({}, started)
    tabs = track_tabs(PINNED_TABS, started)
    assert tabs == tuple(
        replace(tab, running=1) if tab.key == "coder" else tab for tab in PINNED_TABS
    )
    assert tab_labels(tabs) == [
        "planner",
        "experimenter",
        "● coder",
        "reviewer",
        "super-reviewer",
        "configurator",
    ]
    assert track_tabs(tabs, exited, roles=roles) == PINNED_TABS


def test_a_roles_marker_stays_until_its_last_agent_exits() -> None:
    started_1, exited_1 = _pinned("agent-1", "reviewer")
    started_2, exited_2 = _pinned("agent-2", "reviewer")
    roles: Mapping[str, str] = {}
    tabs = PINNED_TABS
    for started in (started_1, started_2):
        roles = track_roles(roles, started)
        tabs = track_tabs(tabs, started, roles=roles)
    assert tab_labels(tabs)[3] == "● reviewer"
    tabs = track_tabs(tabs, exited_1, roles=roles)
    assert tab_labels(tabs)[3] == "● reviewer"
    tabs = track_tabs(tabs, exited_2, roles=roles)
    assert tab_labels(tabs)[3] == "reviewer"
    assert tabs == PINNED_TABS


def test_the_planner_agents_start_and_exit_dont_mark_its_tab() -> None:
    started, exited = _pinned("planner-agent", "planner")
    roles = track_roles({}, started)
    assert track_tabs(PINNED_TABS, started) == PINNED_TABS
    assert track_tabs(PINNED_TABS, exited, roles=roles) == PINNED_TABS


def test_the_planner_tab_is_marked_while_the_planner_is_busy() -> None:
    busy = mark_lane_busy(PINNED_TABS, "planner", True)
    assert busy == (replace(PINNED_TABS[0], running=1), *PINNED_TABS[1:])
    assert tab_labels(busy)[0] == "● planner"
    assert mark_lane_busy(busy, "planner", True) == busy
    assert mark_lane_busy(busy, "planner", False) == PINNED_TABS


def test_only_the_tabs_of_the_roles_answering_in_a_lane_follow_it() -> None:
    tabs = pin_conversations(PINNED_TABS, {"architect": "design", "critic": "design"})
    busy = mark_lane_busy(tabs, "design", True)
    assert tab_labels(busy)[-2:] == ["● architect", "● critic"]
    assert busy[:6] == PINNED_TABS
    assert mark_lane_busy(tabs, "work", True) == tabs


def test_conversational_roles_are_pinned_after_the_built_in_ones_before_the_agents() -> None:
    own = ConversationTab("agent-1", "checker")
    tabs = pin_conversations((*PINNED_TABS, own), {PLANNER: "plans", "architect": "design"})
    architect = ConversationTab("architect", "architect", pinned=True, lane="design")
    assert tabs == (replace(PINNED_TABS[0], lane="plans"), *PINNED_TABS[1:], architect, own)
    assert pinned_roles(tabs) == frozenset({*ROLE_TABS, "architect"})


def test_the_planner_keeps_its_own_lane_when_the_roles_dont_list_it() -> None:
    assert pin_conversations(PINNED_TABS, {}) == PINNED_TABS


def test_a_built_in_role_made_conversational_follows_its_lane() -> None:
    tabs = pin_conversations(PINNED_TABS, {"reviewer": "review"})
    assert tabs[3] == replace(PINNED_TABS[3], lane="review")


def test_pinning_again_keeps_the_tabs_still_listed_and_drops_the_others() -> None:
    tabs = pin_conversations(PINNED_TABS, {"architect": "design", "critic": "review"})
    tabs = mark_tab_unread(mark_lane_busy(tabs, "design", True), "architect", None)
    kept = pin_conversations(tabs, {"architect": "design"})
    assert kept == (*PINNED_TABS, replace(tabs[6], running=1, unread=True))


def test_a_conversational_roles_agents_share_its_tab_without_counting() -> None:
    tabs = pin_conversations(PINNED_TABS, {"architect": "design"})
    started = AgentStarted(agent_id="agent-1", role="architect", task_id="plan")
    roles = track_roles({}, started, pinned_roles(tabs))
    assert roles == {"agent-1": "architect"}
    assert track_tabs(tabs, started, roles=roles) == tabs
    exited = AgentExited(agent_id="agent-1", reason="finished")
    assert track_tabs(tabs, exited, roles=roles) == tabs
    running = (RunningAgent(agent_id="agent-1", role="architect", task_id="plan"),)
    assert running_roles(running, pinned_roles(tabs)) == {"agent-1": "architect"}
    assert follow_running(tabs, running) == tabs


def test_resetting_keeps_the_conversational_roles_tabs_busy_as_they_are() -> None:
    tabs = mark_lane_busy(pin_conversations(PINNED_TABS, {"architect": "design"}), "design", True)
    assert reset_tabs((*tabs, ConversationTab("agent-4", "checker"))) == tabs


def test_the_running_marker_leads_and_the_unread_marker_trails() -> None:
    tabs = (ConversationTab("coder", "coder", unread=True, running=1, pinned=True),)
    assert tab_labels(tabs) == ["● coder *"]
    assert tab_labels(mark_tab_read(tabs, "coder")) == ["● coder"]
    assert tab_labels(mark_lane_busy((PINNED_TABS[0],), "planner", True)) == ["● planner"]
    unread = track_tabs(tabs, AgentStarted(agent_id="a", role="coder", task_id="1"))
    assert tab_labels(unread) == ["● coder *"]


def test_a_tasks_notes_are_keyed_to_its_roles_pinned_tab() -> None:
    for role in ROLE_TABS:
        assert task_tab_key(TaskState(id="1", task=TaskSpec(prompt="p", role=role))) == role


def test_a_task_in_a_role_with_no_pinned_tab_has_no_tab_key() -> None:
    assert task_tab_key(TaskState(id="1", task=TaskSpec(prompt="p", role="checker"))) is None


def test_an_unknown_task_has_no_tab_key() -> None:
    assert task_tab_key(None) is None


def test_marking_a_tab_unread_skips_the_shown_tab_and_all() -> None:
    marked = mark_tab_unread(PINNED_TABS, "coder", active="reviewer")
    assert marked == tuple(
        replace(tab, unread=True) if tab.key == "coder" else tab for tab in PINNED_TABS
    )
    assert mark_tab_unread(PINNED_TABS, "coder", active="coder") == PINNED_TABS
    assert mark_tab_unread(PINNED_TABS, None, active=None) == PINNED_TABS


def test_resetting_tabs_keeps_the_pinned_ones_read_and_idle_but_a_busy_planner() -> None:
    busy = mark_lane_busy(PINNED_TABS, "planner", True)
    tabs = (*busy, ConversationTab("agent-4", "checker"))
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-3", role="coder", task_id="1"))
    tabs = mark_tab_unread(tabs, "coder", None)

    assert reset_tabs(tabs) == busy


def _running(agent: str, role: str) -> RunningAgent:
    return RunningAgent(agent_id=agent, role=role, task_id="1")


def test_running_agents_in_pinned_roles_are_known_by_role() -> None:
    agents = (_running("agent-1", "coder"), _running("agent-2", "checker"))
    assert running_roles(agents) == {"agent-1": "coder"}


def test_following_no_running_agents_closes_own_tabs_and_idles_pinned_but_the_planner() -> None:
    busy = mark_lane_busy(PINNED_TABS, "planner", True)
    tabs = (*busy, ConversationTab("agent-4", "checker", unread=True))
    tabs = track_tabs(tabs, AgentStarted(agent_id="agent-3", role="coder", task_id="1"))
    assert follow_running(tabs, ()) == busy


def test_following_running_agents_counts_each_pinned_roles_agents() -> None:
    agents = (_running("a", "reviewer"), _running("b", "reviewer"), _running("p", PLANNER))
    tabs = follow_running(PINNED_TABS, agents)
    assert tab_labels(tabs) == [
        "planner",
        "experimenter",
        "coder",
        "● reviewer",
        "super-reviewer",
        "configurator",
    ]
    assert tabs[3].running == 2


def test_following_running_agents_keeps_their_open_tabs_and_opens_the_missing() -> None:
    kept = ConversationTab("agent-4", "checker", unread=True)
    tabs = (*PINNED_TABS, ConversationTab("agent-5", "linter"), kept)
    agents = (_running("agent-6", "linter"), _running("agent-4", "checker"))
    assert follow_running(tabs, agents) == (
        *PINNED_TABS,
        kept,
        ConversationTab("agent-6", "linter"),
    )


def test_stopped_callers_are_those_calling_in_the_events_but_not_running_once_each() -> None:
    def call(agent: str) -> ToolCallStarted:
        return ToolCallStarted(turn_id="t", agent_id=agent, call_id="c", name="Read", arguments={})

    request = AgentRequest(agent_id="agent-2", request_id="r", tool="get_task", arguments={})
    events: list[Event] = [call("agent-1"), request, call("agent-3"), call("agent-1")]
    events.append(Notice(message="n"))
    running = (RunningAgent(agent_id="agent-3", role="coder", task_id="1"),)
    assert stopped_callers(events, running) == ("agent-1", "agent-2")
