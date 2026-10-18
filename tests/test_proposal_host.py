"""The ProposalHost tracking which proposal's modal is shown, and when to close it."""

from textual.app import App

from orbit.protocol import AgentExited, ChecksProposed, DecideProposal, ProposedStep
from orbit.tui.proposals import Decided, ProposalHost, ProposalScreen

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="1", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)
OTHER = PROPOSAL.model_copy(update={"agent_id": "agent-4", "task_id": "2"})


class Host(App[None]):
    """An app holding a ProposalHost, recording the decisions it passes on."""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[tuple[ChecksProposed, DecideProposal]] = []
        self.host = ProposalHost(self.push_screen, self._decided)

    async def _decided(self, proposal: ChecksProposed, decision: DecideProposal) -> None:
        self.decisions.append((proposal, decision))

    def proposal_screens(self) -> list[ProposalScreen]:
        return [screen for screen in self.screen_stack if isinstance(screen, ProposalScreen)]


async def test_the_same_proposal_twice_opens_one_screen() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.follow((PROPOSAL,))
        app.host.show(PROPOSAL)
        await pilot.pause()
        [screen] = app.proposal_screens()
        assert app.host.shown is screen


async def test_a_snapshot_dropping_the_shown_proposal_closes_it_and_opens_the_next() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.host.follow((OTHER, PROPOSAL.model_copy(update={"task_id": "3"})))
        await pilot.pause()
        [screen] = app.proposal_screens()
        assert screen.proposal == OTHER
        assert app.host.shown is screen
        assert app.decisions == []


async def test_a_snapshot_with_none_waiting_closes_the_shown_and_opens_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.host.follow(())
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.host.shown is None


async def test_its_agent_exiting_closes_it_and_others_leave_it() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.host.withdraw(AgentExited(agent_id="agent-4", reason="finished"))
        app.host.withdraw(OTHER)
        await pilot.pause()
        assert len(app.proposal_screens()) == 1
        app.host.withdraw(AgentExited(agent_id="agent-3", reason="finished"))
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.host.shown is None
        app.host.withdraw(AgentExited(agent_id="agent-3", reason="finished"))


async def test_a_superseded_screens_late_decision_leaves_the_newer_tracked() -> None:
    pushed: list[tuple[ProposalScreen, Decided]] = []
    decisions: list[tuple[ChecksProposed, DecideProposal]] = []

    async def decide(proposal: ChecksProposed, decision: DecideProposal) -> None:
        decisions.append((proposal, decision))

    host = ProposalHost(lambda screen, decided: pushed.append((screen, decided)), decide)
    host.show(PROPOSAL)
    host.show(OTHER)
    (_, first_decided), (second, _) = pushed
    accept = DecideProposal(task_id="1", action="accept")
    await first_decided(accept)
    assert host.shown is second
    assert decisions == [(PROPOSAL, accept)]


async def test_a_decision_clears_the_shown_proposal() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        end = DecideProposal(task_id="1", action="end")
        app.screen.dismiss(end)
        await pilot.pause()
        assert app.host.shown is None
        assert app.decisions == [(PROPOSAL, end)]
