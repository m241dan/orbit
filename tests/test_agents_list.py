"""The agents running, without the pane showing them: their rows, when they're shown again,
the review line, and which agent cancelling cancels."""

from orbit.protocol import (
    STARTUP_TASK,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    Notice,
    ReviewStatus,
    TurnFinished,
    Usage,
)
from orbit.tui.view.agents import AgentsList
from tests.view_fakes import AgentsView

CODER = AgentStarted(agent_id="agent-1", role="coder", task_id="7")
REVIEWER = AgentStarted(agent_id="agent-2", role="reviewer", task_id="8")


def listed(*events: AgentStarted) -> tuple[AgentsList, AgentsView]:
    """An agents list shown in a view, following ``events``."""
    view = AgentsView()
    agents = AgentsList(view)
    agents.show()
    for event in events:
        agents.track(event)
    return agents, view


def test_with_no_agents_the_list_shows_the_empty_row() -> None:
    _, view = listed()
    assert view.listings == [[]]


def test_the_list_shows_running_agents_by_their_tasks_numbers_and_the_review_status() -> None:
    view = AgentsView()
    agents = AgentsList(view)
    agents.number_tasks({"7": "1", "8": "2"})
    agents.track(CODER)
    agents.track(REVIEWER)
    agents.track(
        AgentRequest(agent_id="agent-2", request_id="r", tool="complete_task", arguments={})
    )
    agents.show_review(ReviewStatus(paused=False, open_batches=1))
    assert view.listings[-1] == [
        "agent-1 coder · task 1 · working",
        "agent-2 reviewer · task 2 · waiting on complete_task",
    ]
    assert view.keys == ["agent-1", "agent-2"]
    assert view.review == "review: active · 1 open"
    agents.track(AgentReply(agent_id="agent-2", request_id="r", text="ok"))
    agents.track(AgentExited(agent_id="agent-1", reason="finished"))
    assert view.listings[-1] == ["agent-2 reviewer · task 2 · working"]


def test_an_event_that_leaves_the_agents_as_they_are_shows_nothing_again() -> None:
    agents, view = listed(CODER)
    shown = len(view.listings)
    agents.track(Notice(message="hello"))
    agents.track(CODER)
    assert len(view.listings) == shown


def test_cancelling_asks_to_cancel_the_highlighted_agent() -> None:
    agents, view = listed(CODER, REVIEWER)
    view.highlighted = 1
    agents.cancel()
    assert view.cancels == ["agent-2"]


def test_cancelling_with_none_highlighted_or_only_the_empty_row_cancels_nothing() -> None:
    agents, view = listed()
    view.highlighted = 0  # The empty row.
    agents.cancel()
    agents.track(CODER)
    view.highlighted = None
    agents.cancel()
    assert view.cancels == []


def test_cancelling_after_an_agent_above_exits_cancels_the_agent_now_highlighted() -> None:
    agents, view = listed(
        CODER,
        REVIEWER,
        AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK),
    )
    agents.track(AgentExited(agent_id="agent-1", reason="finished"))
    view.highlighted = 0  # Where the pane keeps the highlight: on agent-2's row.
    agents.cancel()
    assert view.cancels == ["agent-2"]


def test_the_startup_check_is_listed_against_the_base_commit_and_can_be_cancelled() -> None:
    agents, view = listed(
        AgentStarted(agent_id="startup-check", role="checker", task_id=STARTUP_TASK)
    )
    assert view.listings[-1] == ["startup-check checker · base commit · working"]
    view.highlighted = 0
    agents.cancel()
    assert view.cancels == ["startup-check"]


def test_the_agents_are_shown_again_only_when_their_numbers_change() -> None:
    agents, view = listed(AgentStarted(agent_id="agent-1", role="coder", task_id="2"))
    agents.number_tasks({"1": "1", "2": "2"})
    assert view.listings[-1] == ["agent-1 coder · task 2 · working"]
    shown = len(view.listings)
    # A task added after agent-1's leaves its number alone.
    agents.number_tasks({"1": "1", "2": "2", "3": "3"})
    assert len(view.listings) == shown
    # One added before it renumbers it.
    agents.number_tasks({"3": "1", "1": "2", "2": "3"})
    assert view.listings[-1] == ["agent-1 coder · task 3 · working"]


def test_no_review_shows_no_review_line() -> None:
    agents, view = listed()
    agents.show_review(ReviewStatus(paused=True, open_batches=0))
    assert view.review == "review: paused"
    agents.show_review(None)
    assert view.review == ""


def test_a_row_ends_with_its_agents_cost_once_its_first_turn_ends() -> None:
    agents, view = listed(CODER, REVIEWER)
    assert view.listings[-1][0] == "agent-1 coder · task 7 · working"
    agents.track(TurnFinished(turn_id="t", agent_id="agent-1", reason="completed"))
    for _ in range(2):
        agents.track(
            TurnFinished(
                turn_id="t", agent_id="agent-1", reason="completed", usage=Usage(cost_usd=0.2)
            )
        )
    assert view.listings[-1] == [
        "agent-1 coder · task 7 · working · $0.40",
        "agent-2 reviewer · task 8 · working",
    ]
