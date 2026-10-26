"""The consultations held for approval, asked about one at a time, each once, and closed once
settled, by this frontend or another."""

from orbit.protocol import PLANNER, ApprovalDecided, ApprovalRequested, DecideApproval
from orbit.tui.view.approvals import ApprovalQueue
from tests.view_fakes import ApprovalModal


def request(number: int) -> ApprovalRequested:
    return ApprovalRequested(
        consultation=number, asker=PLANNER, consultant="experimenter", title="Check X", asked=6
    )


class Queue:
    """An :class:`ApprovalQueue` over an :class:`ApprovalModal`, with the decisions sent."""

    def __init__(self) -> None:
        self.modal = ApprovalModal()
        self.sent: list[DecideApproval] = []
        self.queue = ApprovalQueue(self.modal, self._send)

    async def _send(self, decision: DecideApproval) -> None:
        self.sent.append(decision)


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
