"""The consultations and the agents' calls held for approval, asked about one at a time, each
once, in the words the modal shows, and closed once settled, by this frontend or another."""

import json

import pytest

from orbit.protocol import (
    PLANNER,
    ApprovalDecided,
    ApprovalDecision,
    ApprovalRequested,
    DecideApproval,
    DecideToolCall,
    ToolCallDecided,
    ToolCallHeld,
)
from orbit.tui.view.approvals import (
    APPROVAL_TITLE,
    TOOL_CALL_TITLE,
    ApprovalQueue,
    held_choices,
    held_text,
    held_title,
    shown_arguments,
)
from tests.view_fakes import ApprovalModal


def request(number: int) -> ApprovalRequested:
    return ApprovalRequested(
        consultation=number, asker=PLANNER, consultant="experimenter", title="Check X", asked=6
    )


class Queue:
    """An :class:`ApprovalQueue` over an :class:`ApprovalModal`, with the decisions sent."""

    def __init__(self) -> None:
        self.modal = ApprovalModal()
        self.sent: list[DecideApproval | DecideToolCall] = []
        self.queue = ApprovalQueue(self.modal, self._send)

    async def _send(self, decision: DecideApproval | DecideToolCall) -> None:
        self.sent.append(decision)


def call(number: int, **arguments: object) -> ToolCallHeld:
    return ToolCallHeld(
        call=number,
        agent_id="agent-1",
        role="coder",
        server="github",
        tool="github__create_issue",
        arguments=arguments or {"title": "Flaky test"},
    )


def test_one_is_asked_at_a_time_each_once() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.show(request(2))
    queue.queue.show(request(2))
    queue.queue.show(request(1))
    assert queue.modal.opened == [request(1)]


async def test_deciding_sends_it_and_asks_the_next() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.show(request(2))
    await queue.modal.decide("allow_hour")
    assert queue.sent == [DecideApproval(consultation=1, decision="allow_hour")]
    assert queue.modal.shown == request(2)


async def test_closing_undecided_sends_nothing() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    await queue.modal.decide(None)
    assert queue.sent == []


def test_another_frontends_decision_closes_the_one_shown_and_asks_the_next() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.show(request(2))
    queue.queue.settle(ApprovalDecided(consultation=1, decision="allow"))
    assert queue.modal.closed == [request(1)]
    assert queue.modal.shown == request(2)


def test_a_decision_on_one_waiting_drops_it() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.show(request(2))
    queue.queue.settle(ApprovalDecided(consultation=2, decision="deny"))
    queue.queue.settle(ApprovalDecided(consultation=1, decision="deny"))
    assert (queue.modal.closed, queue.modal.shown) == ([request(1)], None)


def test_a_decision_with_none_shown_closes_nothing() -> None:
    queue = Queue()
    queue.queue.settle(ApprovalDecided(consultation=1, decision="deny"))
    assert queue.modal.closed == []


def test_following_a_snapshot_closes_one_no_longer_held_and_asks_those_held() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.follow((request(2), request(3)))
    assert queue.modal.closed == [request(1)]
    assert queue.modal.opened == [request(1), request(2)]


def test_following_keeps_the_one_shown_if_still_held() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.follow((request(1),))
    assert (queue.modal.opened, queue.modal.closed) == ([request(1)], [])


def test_following_none_held_with_none_shown_asks_nothing() -> None:
    queue = Queue()
    queue.queue.follow(())
    assert queue.modal.opened == []


# Agents' calls


def test_a_held_call_is_asked_about_with_its_role_tool_and_arguments() -> None:
    assert held_text(call(1)) == (
        'The coder wants to call github__create_issue with {"title": "Flaky test"}.\n'
        "\n"
        "a  Allow\n"
        "d  Deny"
    )
    assert held_title(call(1)) == TOOL_CALL_TITLE
    assert [key for key, _, _ in held_choices(call(1))] == ["a", "d"]


def test_a_consultation_is_asked_about_with_its_three_choices() -> None:
    assert held_title(request(1)) == APPROVAL_TITLE
    assert [key for key, _, _ in held_choices(request(1))] == ["a", "h", "d"]
    assert held_text(request(1)).startswith("The planner wants to ask the experimenter")


def test_long_arguments_are_cut_short_and_short_ones_kept() -> None:
    shown = shown_arguments({"title": "t" * 100, "labels": ["bug"], "body": {"text": "b" * 80}})
    assert json.loads(shown) == {
        "title": "t" * 59 + "…",
        "labels": ["bug"],
        "body": '{"text": "' + "b" * 49 + "…",
    }


def test_calls_queue_behind_consultations_in_one_modal() -> None:
    queue = Queue()
    queue.queue.show(request(1))
    queue.queue.show(call(1))
    queue.queue.settle(ApprovalDecided(consultation=1, decision="deny"))
    assert queue.modal.shown == call(1)


@pytest.mark.parametrize(
    ("decision", "answer"), [("allow", "allow"), ("deny", "deny"), ("allow_hour", "deny")]
)
async def test_deciding_on_a_call_sends_its_decision(
    decision: ApprovalDecision, answer: str
) -> None:
    queue = Queue()
    queue.queue.show(call(3))
    await queue.modal.decide(decision)
    assert queue.sent == [DecideToolCall.model_validate({"call": 3, "decision": answer})]


def test_a_call_settled_elsewhere_closes_and_a_consultation_of_its_number_stays() -> None:
    queue = Queue()
    queue.queue.show(call(1))
    queue.queue.show(request(1))
    queue.queue.settle(ToolCallDecided(call=1, decision="withdrawn"))
    assert queue.modal.closed == [call(1)]
    assert queue.modal.shown == request(1)
