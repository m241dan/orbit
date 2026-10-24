"""The proposals waiting on the user, without Textual: which is shown, which wait, and what
happens to each as the user decides, snapshots come and agents exit."""

from orbit.protocol import (
    AgentExited,
    AssistantMessage,
    ChecksProposed,
    DecideProposal,
    ProposedStep,
)
from orbit.tui.view.proposals import Decided, ProposalQueue

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="1", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)
OTHER = PROPOSAL.model_copy(update={"agent_id": "agent-4", "task_id": "2"})
THIRD = PROPOSAL.model_copy(update={"agent_id": "agent-5", "task_id": "3"})
ACCEPT = DecideProposal(task_id="1", action="accept")


class Modal:
    """Where a :class:`ProposalQueue` shows proposals, as the app's modal slot does: what it
    opened, in turn, what it closed, the proposal shown, and the decisions passed on. Only
    the proposal shown can be decided: a closed one's screen answers nothing."""

    def __init__(self) -> None:
        self.opened: list[ChecksProposed] = []
        self.closed: list[ChecksProposed] = []
        self.shown: ChecksProposed | None = None
        self.decisions: list[tuple[ChecksProposed, DecideProposal]] = []
        self._decided: Decided | None = None
        self.queue = ProposalQueue(self, self._decide)

    async def _decide(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        self.decisions.append((proposal, decision))

    def open(self, proposal: ChecksProposed, decided: Decided) -> None:
        self.opened.append(proposal)
        self.shown, self._decided = proposal, decided

    def close(self) -> None:
        if self.shown is not None:
            self.closed.append(self.shown)
        self.shown = self._decided = None

    async def decide(self, decision: DecideProposal | None) -> None:
        """Have the user decide on the proposal shown, or close it undecided."""
        decided = self._decided
        assert decided is not None
        self.shown = self._decided = None
        await decided(decision)


def exited(agent: str) -> AgentExited:
    return AgentExited(agent_id=agent, reason="finished")


def test_the_same_proposal_twice_is_shown_once() -> None:
    modal = Modal()
    modal.queue.follow((PROPOSAL,))
    modal.queue.show(PROPOSAL)
    assert (modal.opened, modal.shown) == ([PROPOSAL], PROPOSAL)


def test_a_snapshot_without_the_shown_proposal_closes_it_and_shows_the_next() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.follow((OTHER, THIRD))
    assert modal.closed == [PROPOSAL]
    assert (modal.opened, modal.shown) == ([PROPOSAL, OTHER], OTHER)
    assert modal.decisions == []


def test_a_snapshot_with_none_waiting_closes_the_shown_and_shows_nothing() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.follow(())
    assert (modal.closed, modal.shown) == ([PROPOSAL], None)


def test_its_agent_exiting_closes_it_and_another_agent_exiting_leaves_it() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.withdraw(exited("agent-4"))
    assert modal.closed == []
    modal.queue.withdraw(exited("agent-3"))
    assert (modal.closed, modal.shown) == ([PROPOSAL], None)
    modal.queue.withdraw(exited("agent-3"))
    assert modal.closed == [PROPOSAL]


async def test_an_agents_other_events_leave_its_proposals_shown_and_waiting() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.show(OTHER)
    for agent in ("agent-3", "agent-4"):
        modal.queue.withdraw(AssistantMessage(turn_id="t", text="hi", agent_id=agent))
    assert modal.shown == PROPOSAL
    await modal.decide(None)
    assert modal.shown == OTHER
    await modal.decide(ACCEPT)
    modal.queue.reopen(OTHER)
    assert modal.opened == [PROPOSAL, OTHER, OTHER]


async def test_a_second_proposal_waits_until_the_first_is_decided() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.show(OTHER)
    modal.queue.show(OTHER)
    assert modal.opened == [PROPOSAL]
    await modal.decide(ACCEPT)
    assert (modal.opened, modal.shown) == ([PROPOSAL, OTHER], OTHER)
    assert modal.decisions == [(PROPOSAL, ACCEPT)]
    await modal.decide(None)
    assert (modal.opened, modal.shown) == ([PROPOSAL, OTHER], None)
    assert modal.decisions == [(PROPOSAL, ACCEPT)]


def test_withdrawing_the_shown_proposal_shows_the_next_waiting() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.show(OTHER)
    modal.queue.withdraw(exited("agent-3"))
    assert (modal.opened, modal.shown) == ([PROPOSAL, OTHER], OTHER)


async def test_an_agent_exiting_drops_its_waiting_proposal() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.show(OTHER)
    modal.queue.withdraw(exited("agent-4"))
    await modal.decide(None)
    assert (modal.opened, modal.shown) == ([PROPOSAL], None)


async def test_a_snapshot_replaces_the_waiting_proposals() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    modal.queue.show(OTHER)
    modal.queue.follow((PROPOSAL, THIRD, THIRD))
    await modal.decide(None)
    assert modal.opened == [PROPOSAL, THIRD]
    await modal.decide(None)
    assert (modal.opened, modal.shown) == ([PROPOSAL, THIRD], None)


async def test_reopening_a_decided_proposal_shows_it_again() -> None:
    modal = Modal()
    modal.queue.show(PROPOSAL)
    await modal.decide(ACCEPT)
    modal.queue.reopen(PROPOSAL)
    assert (modal.opened, modal.shown) == ([PROPOSAL, PROPOSAL], PROPOSAL)


async def test_reopening_while_another_is_shown_shows_it_next() -> None:
    modal = Modal()
    modal.queue.show(OTHER)
    modal.queue.show(THIRD)
    modal.queue.show(PROPOSAL)
    modal.queue.reopen(PROPOSAL)
    modal.queue.reopen(OTHER)  # Shown, so it isn't queued again.
    await modal.decide(None)
    assert modal.opened == [OTHER, PROPOSAL]
    await modal.decide(None)
    assert modal.opened == [OTHER, PROPOSAL, THIRD]
    await modal.decide(None)
    assert modal.opened == [OTHER, PROPOSAL, THIRD]


def test_a_proposal_of_an_agent_that_exited_is_not_reopened_until_a_snapshot() -> None:
    modal = Modal()
    modal.queue.withdraw(exited("agent-3"))
    modal.queue.reopen(PROPOSAL)
    assert modal.opened == []
    modal.queue.follow(())
    modal.queue.reopen(PROPOSAL)
    assert modal.opened == [PROPOSAL]


async def test_a_new_proposal_from_an_agent_that_exited_counts_it_as_back() -> None:
    modal = Modal()
    modal.queue.withdraw(exited("agent-3"))
    modal.queue.show(PROPOSAL)  # Agent ids restart with orbit.
    await modal.decide(ACCEPT)
    modal.queue.reopen(PROPOSAL)
    assert (modal.opened, modal.shown) == ([PROPOSAL, PROPOSAL], PROPOSAL)
