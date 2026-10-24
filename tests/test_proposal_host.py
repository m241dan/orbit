"""The ProposalHost showing its queue's proposals as modal screens; what the queue decides is
checked in test_proposal_queue."""

from textual.app import App

from orbit.protocol import AgentExited, ChecksProposed, DecideProposal, ProposedStep
from orbit.tui.proposals import ProposalHost, ProposalScreen
from orbit.tui.view.proposals import Decided

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="1", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)
OTHER = PROPOSAL.model_copy(update={"agent_id": "agent-4", "task_id": "2"})


class Host(App[None]):
    """An app holding a ProposalHost, recording the decisions it passes on, and the callback
    of each screen it pushes."""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[tuple[ChecksProposed, DecideProposal]] = []
        self.callbacks: list[Decided] = []
        self.host = ProposalHost(self._push, self._decided)

    def _push(self, screen: ProposalScreen, decided: Decided) -> None:
        self.callbacks.append(decided)
        self.push_screen(screen, decided)

    async def _decided(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        self.decisions.append((proposal, decision))

    def proposal_screens(self) -> list[ProposalScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, ProposalScreen)]


async def test_a_proposal_shows_as_a_screen_whose_decision_is_passed_on() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.queue.show(PROPOSAL)
        await pilot.pause()
        [screen] = app.proposal_screens()
        assert screen.proposal == PROPOSAL
        assert app.host.shown == PROPOSAL
        end = DecideProposal(task_id="1", action="end")
        screen.dismiss(end)
        await pilot.pause()
        assert app.host.shown is None
        assert app.decisions == [(PROPOSAL, end)]


async def test_its_agent_exiting_closes_its_screen() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.queue.show(PROPOSAL)
        await pilot.pause()
        app.host.queue.withdraw(AgentExited(agent_id="agent-3", reason="finished"))
        assert app.host.shown is None
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.decisions == []


async def test_a_superseded_screens_late_decision_sends_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.queue.show(PROPOSAL)
        await pilot.pause()
        app.host.queue.follow((OTHER,))
        await pilot.pause()
        [second] = app.proposal_screens()
        await app.callbacks[0](DecideProposal(task_id="1", action="accept"))
        await pilot.pause()
        assert app.decisions == []
        assert app.host.shown == OTHER
        assert app.proposal_screens() == [second]
