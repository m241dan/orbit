"""Agents grouped by their trigger's tab: the tabs the catalog's triggers pin, the built-in roles'
tabs they replace, the agents writing into them, labelled by role, and their markers, live and
replayed."""

from orbit.agent.domain.catalog import trigger_entries
from orbit.protocol import (
    BUGS_REVIEWER,
    PLANNER,
    TESTS_REVIEWER,
    AgentExited,
    AgentStarted,
    AssistantMessage,
    Catalog,
    Event,
    EventEnvelope,
    RunningAgent,
    TriggerEntry,
)
from orbit.tui import OrbitApp
from orbit.tui.view.tabs import (
    PINNED_TABS,
    ConversationTab,
    follow_running,
    pin_conversations,
    pin_trigger_tabs,
    pinned_key,
    running_roles,
    running_speakers,
    tabbed_roles,
    track_roles,
    track_speakers,
    track_tabs,
    trigger_tab_key,
    trigger_tabs,
)
from orbit.tui.view.transcript import speaker
from tests.agent_fakes import ROLES, SHIPPED_TRIGGERS
from tests.frontend_fakes import Shell
from tests.tui_fakes import SNAPSHOT, FakeSession, feed_events, tab_strip

EMPTY = SNAPSHOT.model_copy(update={"busy": {}})
"""A snapshot with no lane busy."""

SHIPPED = trigger_entries(SHIPPED_TRIGGERS, ROLES)
"""The catalog's triggers with the shipped configuration."""

CATALOG = Catalog(workspaces=(), agents=(), triggers=SHIPPED, lanes=())
"""A catalog listing the shipped triggers."""

STRIP = [
    "All",
    "planner",
    "experimenter",
    "coder",
    "configurator",
    "review",
    "super-review",
]
"""The strip with the shipped triggers: no reviewer tab, and one per trigger tab."""

REVIEW = trigger_tab_key("review")
"""The key of the review tab."""

LENS = TriggerEntry(name="bugs-review", label="b", manual=False, tab="review", roles=("lens",))
UNTABBED = TriggerEntry(name="fix", label="f", manual=False, roles=("coder",))
MERGE = TriggerEntry(name="merge", label="m", manual=False, tab="review", roles=("reviewer",))
SUPER = TriggerEntry(name="super", label="s", manual=False, tab="super", roles=("lens",))


def started(agent: str, role: str, tab: str | None = None) -> AgentStarted:
    return AgentStarted(agent_id=agent, role=role, task_id="1", tab=tab)


def running(agent: str, role: str, tab: str | None = None) -> RunningAgent:
    return RunningAgent(agent_id=agent, role=role, task_id="1", tab=tab)


def said(agent: str, text: str = "hmm") -> AssistantMessage:
    return AssistantMessage(turn_id="t", text=text, agent_id=agent)


def exited(agent: str) -> AgentExited:
    return AgentExited(agent_id=agent, reason="finished")


def grouped(*triggers: TriggerEntry) -> tuple[ConversationTab, ...]:
    """The built-in pinned tabs, grouped by ``triggers``."""
    return pin_trigger_tabs(PINNED_TABS, triggers)


# The tabs the triggers name, and the roles they take over


def test_the_triggers_tabs_are_each_named_once_in_their_order() -> None:
    assert trigger_tabs((LENS, UNTABBED, SUPER, MERGE)) == ("review", "super")


def test_a_role_only_tabbed_triggers_queue_is_tabbed_and_one_an_untabbed_one_queues_isnt() -> None:
    fixing = TriggerEntry(name="fix-review", label="r", manual=False, roles=("reviewer",))
    assert tabbed_roles((LENS, UNTABBED, MERGE)) == {"lens", "reviewer"}
    assert tabbed_roles((LENS, MERGE, fixing)) == {"lens"}


def test_the_shipped_triggers_drop_the_reviewer_tab_and_pin_review_and_super_review() -> None:
    assert [tab.key for tab in grouped(*SHIPPED)] == [
        "planner",
        "experimenter",
        "coder",
        "configurator",
        REVIEW,
        trigger_tab_key("super-review"),
    ]


def test_a_trigger_tab_is_labelled_by_its_name_and_counts_no_agents_yet() -> None:
    assert grouped(LENS)[-1] == ConversationTab(REVIEW, "review", pinned=True)


def test_a_conversational_built_in_role_keeps_its_tab_even_if_only_tabbed_triggers_queue_it() -> (
    None
):
    planning = TriggerEntry(name="plan", label="p", manual=False, tab="plans", roles=(PLANNER,))
    assert [tab.key for tab in grouped(planning)][:1] == [PLANNER]


def test_grouping_again_restores_a_built_in_tab_and_drops_tabs_no_longer_named() -> None:
    again = pin_trigger_tabs(grouped(MERGE), (UNTABBED,))
    assert again == PINNED_TABS


def test_grouping_keeps_other_conversations_and_the_agents_own_tabs_after_the_trigger_tabs() -> (
    None
):
    tabs = (*pin_conversations(PINNED_TABS, {"architect": "architect"}), ConversationTab("a", "x"))
    keys = [tab.key for tab in pin_trigger_tabs(tabs, (LENS,))]
    assert keys[-3:] == [REVIEW, "architect", "a"]


def test_pinning_conversations_keeps_the_trigger_tabs_before_them() -> None:
    tabs = pin_conversations(grouped(LENS), {"architect": "architect"})
    assert [tab.key for tab in tabs][-2:] == [REVIEW, "architect"]


# Which tab an agent writes into


def test_an_agent_writes_into_its_triggers_tab_when_its_pinned() -> None:
    assert pinned_key("lens", "review", {REVIEW, "coder"}) == REVIEW


def test_an_agent_whose_tab_isnt_pinned_writes_into_its_roles_tab_or_its_own() -> None:
    assert pinned_key("coder", "review", {"coder"}) == "coder"
    assert pinned_key("lens", "review", {"coder"}) is None
    assert pinned_key("lens", None, {REVIEW}) is None


def test_an_agent_starting_in_a_trigger_tab_counts_there_and_marks_it_unread() -> None:
    tabs = track_tabs(grouped(LENS), started("agent-1", "lens", "review"))
    roles = track_roles({}, started("agent-1", "lens", "review"), {REVIEW})
    tabs = track_tabs(tabs, said("agent-1"), roles=roles)
    assert tabs[-1] == ConversationTab(REVIEW, "review", unread=True, running=1, pinned=True)
    tabs = track_tabs(tabs, exited("agent-1"), roles=roles)
    assert tabs[-1].running == 0


def test_following_the_running_agents_counts_those_in_trigger_tabs() -> None:
    agents = [running("agent-1", "lens", "review"), running("agent-2", "other", "review")]
    tabs = follow_running(grouped(LENS), agents)
    assert (tabs[-1].running, len(tabs)) == (2, len(grouped(LENS)))
    assert running_roles(agents, {REVIEW}) == {"agent-1": REVIEW, "agent-2": REVIEW}


def test_only_agents_in_trigger_tabs_are_labelled_by_role() -> None:
    agents = [running("agent-1", "lens", "review"), running("agent-2", "coder")]
    assert running_speakers(agents, {REVIEW, "coder"}) == {"agent-1": "lens"}
    learnt = track_speakers({}, started("agent-3", "lens", "review"), {REVIEW})
    assert track_speakers(learnt, started("agent-4", "coder"), {"coder"}) == {"agent-3": "lens"}


def test_an_agent_given_a_name_is_spoken_by_it_but_the_planner_by_none() -> None:
    assert speaker("agent-1", {}, {"agent-1": "lens"}) == "lens"
    assert speaker("agent-2", {"agent-2": PLANNER}, {"agent-2": "lens"}) is None


# The strip, with the shipped catalog


async def test_with_the_shipped_catalog_the_strip_has_review_tabs_and_no_reviewer() -> None:
    shell = Shell()
    await shell.feed(CATALOG)
    assert shell.tab_strip == STRIP


async def test_a_lens_agents_entries_go_to_the_review_tab_labelled_by_role_marking_it() -> None:
    shell = Shell()
    await shell.feed(CATALOG, started("agent-1", BUGS_REVIEWER, "review"), said("agent-1"))
    assert shell.tab_strip == [*STRIP[:5], "● review *", "super-review"]
    entries = shell.window.conversation.entries(REVIEW)
    assert [entry.text for entry in entries] == ["▶ agent-1 (bugs-reviewer) started", "hmm"]
    assert entries[-1].speaker == BUGS_REVIEWER


async def test_two_lens_agents_running_at_once_both_write_into_the_review_tab() -> None:
    shell = Shell()
    await shell.feed(
        CATALOG,
        started("agent-1", BUGS_REVIEWER, "review"),
        started("agent-2", TESTS_REVIEWER, "review"),
        said("agent-1", "one"),
        said("agent-2", "two"),
    )
    await shell.click_tab("● review *")
    assert shell.shown()[-2:] == ["one", "two"]
    assert shell.tab_strip[5] == "● review"
    await shell.feed(exited("agent-1"))
    assert shell.tab_strip[5] == "● review"
    await shell.feed(exited("agent-2"))
    assert shell.tab_strip[5] == "review"


async def test_an_agent_whose_trigger_has_no_tab_gets_its_own_tab() -> None:
    shell = Shell()
    await shell.feed(CATALOG, started("agent-1", "checker"), said("agent-1"))
    assert shell.tab_strip == [*STRIP, "checker *"]
    assert shell.window.conversation.entries(None)[-1].speaker == "agent-1"


async def test_an_agent_with_a_tab_the_catalog_doesnt_list_gets_its_own_tab() -> None:
    shell = Shell()
    await shell.feed(CATALOG, started("agent-1", "reviewer", "elsewhere"))
    assert shell.tab_strip == [*STRIP, "reviewer"]


async def test_replaying_the_log_puts_past_lens_agents_in_the_review_tab() -> None:
    shell = Shell()
    lens = started("agent-7", BUGS_REVIEWER, "review")
    events: tuple[Event, ...] = (lens, said("agent-7", "found one"))
    logged = tuple(
        EventEnvelope(seq=seq, session="s1", payload=event)
        for seq, event in enumerate(events, start=1)
    )
    await shell.feed(CATALOG, EMPTY.model_copy(update={"transcript": logged, "agents": ()}))
    assert shell.tab_strip == STRIP
    entries = shell.window.conversation.entries(REVIEW)
    assert [(entry.text, entry.speaker) for entry in entries][-1] == ("found one", BUGS_REVIEWER)


async def test_a_lens_agent_running_from_before_the_log_writes_into_the_review_tab() -> None:
    shell = Shell()
    agents = (running("agent-7", BUGS_REVIEWER, "review"),)
    await shell.feed(CATALOG, EMPTY.model_copy(update={"agents": agents}))
    await shell.feed(said("agent-7"))
    assert shell.tab_strip[5] == "● review *"
    assert shell.window.conversation.entries(REVIEW)[-1].speaker == BUGS_REVIEWER


# The widget


async def test_the_widget_shows_the_review_tabs_the_catalog_names() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, CATALOG, started("agent-1", BUGS_REVIEWER, "review"))
        assert tab_strip(app) == [*STRIP[:5], "● review", "super-review"]
