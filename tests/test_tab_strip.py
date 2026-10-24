"""The strip of conversation tabs, without Textual: the tabs it opens, closes and marks as
agents run and talk, and how the user moves between them."""

from orbit.protocol import PLANNER, AgentExited, AgentStarted, AssistantMessage, RunningAgent
from orbit.tui.view.tab_strip import ALL_TAB_ID, OPENING, PLANNER_TAB_ID, TabStrip

STRIP = ["All", "planner", "coder", "reviewer", "super-reviewer", "configurator"]


class Strip:
    """The widget a :class:`TabStrip` shows in: its tabs' labels by id, in order, the active
    one, and the relabels of the active tab it said."""

    def __init__(self) -> None:
        self.labels = dict(OPENING)
        self.active = ALL_TAB_ID
        self.relabels: list[tuple[str | None, str]] = []
        self.tabs = TabStrip(self)

    def activate(self, tab_id: str) -> None:
        assert tab_id in self.labels
        self.active = tab_id

    def open_tab(self, tab_id: str, label: str) -> None:
        assert tab_id not in self.labels
        self.labels[tab_id] = label

    def close_tab(self, tab_id: str) -> None:
        del self.labels[tab_id]

    def label_tab(self, tab_id: str, label: str) -> None:
        self.labels[tab_id] = label

    def relabelled(self, key: str | None, label: str) -> None:
        self.relabels.append((key, label))

    @property
    def shown(self) -> list[str]:
        """The tabs' labels, in order."""
        return list(self.labels.values())

    def id_of(self, label: str) -> str:
        """The id of the tab labelled ``label``."""
        return next(tab_id for tab_id, shown in self.labels.items() if shown == label)


def started(agent: str, role: str) -> AgentStarted:
    return AgentStarted(agent_id=agent, role=role, task_id="1")


def exited(agent: str) -> AgentExited:
    return AgentExited(agent_id=agent, reason="finished")


def said(agent: str) -> AssistantMessage:
    return AssistantMessage(turn_id="t", text="hi", agent_id=agent)


def test_the_strip_starts_with_all_and_a_tab_per_built_in_role() -> None:
    strip = Strip()
    assert strip.shown == STRIP
    assert strip.tabs.agent_of(ALL_TAB_ID) is None
    assert strip.tabs.agent_of(PLANNER_TAB_ID) == PLANNER


def test_an_agent_outside_the_pinned_roles_has_a_tab_of_its_own_until_it_exits() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "tester"))
    strip.tabs.track(started("agent-4", "tester"))
    assert strip.shown == [*STRIP, "tester", "tester 2"]
    tab_id = strip.id_of("tester")
    assert (strip.tabs.agent_of(tab_id), strip.tabs.label_of(tab_id)) == ("agent-3", "tester")
    strip.tabs.track(exited("agent-3"))
    assert strip.shown == [*STRIP, "tester"]
    assert strip.tabs.agent_of(tab_id) is None


def test_closing_the_active_tab_shows_all() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "tester"))
    strip.activate(strip.id_of("tester"))
    strip.tabs.track(exited("agent-3"))
    assert strip.active == ALL_TAB_ID


def test_an_agent_in_a_pinned_role_marks_its_roles_tab_while_it_runs() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "coder"))
    assert strip.shown[2] == "● coder"
    assert strip.tabs.key_of("agent-3") == "coder"
    assert strip.tabs.key_of("agent-9") == "agent-9"
    strip.tabs.track(exited("agent-3"))
    assert strip.shown == STRIP


def test_output_marks_a_tab_unread_unless_it_is_shown_until_it_is_read() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "tester"))
    strip.tabs.track(said("agent-3"))
    assert strip.shown[-1] == "tester *"
    strip.tabs.mark_read(None)
    assert strip.shown[-1] == "tester *"
    strip.tabs.mark_read("agent-3")
    assert strip.shown[-1] == "tester"
    strip.activate(strip.id_of("tester"))
    strip.tabs.track(said("agent-3"))
    assert strip.shown[-1] == "tester"


def test_an_entry_without_an_agent_marks_its_tab_unread_unless_shown() -> None:
    strip = Strip()
    strip.tabs.mark_unread(None)
    assert strip.shown == STRIP
    strip.tabs.mark_unread("coder")
    assert strip.shown[2] == "coder *"
    strip.activate(strip.id_of("reviewer"))
    strip.tabs.mark_unread("reviewer")
    assert strip.shown[3] == "reviewer"


def test_replayed_events_open_and_close_tabs_but_mark_none_unread() -> None:
    strip = Strip()
    strip.tabs.replay(started("agent-3", "tester"))
    strip.tabs.replay(started("agent-4", "coder"))
    strip.tabs.replay(said("agent-3"))
    assert strip.shown == ["All", "planner", "● coder", *STRIP[3:], "tester"]
    assert strip.tabs.key_of("agent-4") == "coder"
    strip.tabs.replay(exited("agent-3"))
    assert strip.shown[-1] == "configurator"


def test_learning_roles_leaves_the_tabs_as_they_are() -> None:
    strip = Strip()
    strip.tabs.learn_roles(started("agent-3", "coder"))
    assert strip.tabs.key_of("agent-3") == "coder"
    assert strip.shown == STRIP


def test_a_reset_closes_the_agents_tabs_and_forgets_their_roles_but_the_running() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "tester"))
    strip.tabs.track(started("agent-4", "coder"))
    strip.tabs.track(started("agent-5", "reviewer"))
    strip.tabs.mark_unread("planner")
    strip.tabs.reset([RunningAgent(agent_id="agent-5", role="reviewer", task_id="1")])
    assert strip.shown == STRIP
    assert strip.tabs.key_of("agent-4") == "agent-4"
    assert strip.tabs.key_of("agent-5") == "reviewer"


def test_following_the_agents_running_opens_their_tabs_and_counts_the_pinned() -> None:
    strip = Strip()
    running = [
        RunningAgent(agent_id="agent-3", role="tester", task_id="1"),
        RunningAgent(agent_id="agent-4", role="coder", task_id="2"),
    ]
    strip.tabs.follow(running)
    assert strip.shown == ["All", "planner", "● coder", *STRIP[3:], "tester"]
    assert strip.tabs.key_of("agent-4") == "coder"


def test_the_planner_tab_is_marked_while_the_planner_is_busy() -> None:
    strip = Strip()
    strip.tabs.set_planner_busy(True)
    assert strip.shown[1] == "● planner"
    assert strip.relabels == []  # The planner's tab isn't the one shown.
    strip.activate(PLANNER_TAB_ID)
    strip.tabs.set_planner_busy(False)
    assert strip.shown[1] == "planner"
    assert strip.relabels == [(PLANNER, "planner")]


def test_toggling_the_planner_shows_it_then_the_tab_shown_before() -> None:
    strip = Strip()
    strip.activate(strip.id_of("coder"))
    strip.tabs.toggle_planner()
    assert strip.active == PLANNER_TAB_ID
    strip.tabs.toggle_planner()
    assert strip.active == strip.id_of("coder")


def test_toggling_back_from_the_planner_shows_all_once_the_tab_before_has_closed() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "tester"))
    strip.activate(strip.id_of("tester"))
    strip.tabs.toggle_planner()
    strip.tabs.track(exited("agent-3"))
    strip.tabs.toggle_planner()
    assert strip.active == ALL_TAB_ID


def test_cycling_moves_through_the_tabs_in_order_and_wraps() -> None:
    strip = Strip()
    strip.tabs.track(started("agent-3", "tester"))
    strip.tabs.cycle(1)
    assert strip.active == PLANNER_TAB_ID
    strip.tabs.cycle(-1)
    strip.tabs.cycle(-1)
    assert strip.active == strip.id_of("tester")
    strip.tabs.cycle(1)
    assert strip.active == ALL_TAB_ID


def test_cycling_with_no_tab_active_shows_the_first_or_the_last() -> None:
    strip = Strip()
    strip.active = ""
    strip.tabs.cycle(1)
    assert strip.active == ALL_TAB_ID
    strip.active = ""
    strip.tabs.cycle(-1)
    assert strip.active == strip.id_of("configurator")
