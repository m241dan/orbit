"""The ProposalHost tracking which proposal's modal is shown, and when to close it."""

from textual.app import App

from orbit.protocol import AgentExited, ChecksProposed, DecideProposal, ProposedStep
from orbit.tui.proposals import Decided, ProposalHost, ProposalScreen
from orbit.tui.widgets import ConfirmScreen

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

    def recording(self) -> list[Decided]:
        """Have the host push through this app, recording each screen's callback."""
        callbacks: list[Decided] = []

        def push(screen: ProposalScreen, decided: Decided) -> None:
            callbacks.append(decided)
            self.push_screen(screen, decided)

        self.host = ProposalHost(push, self._decided)
        return callbacks

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


def recording_host() -> tuple[ProposalHost, list[tuple[ProposalScreen, Decided]]]:
    """A ProposalHost recording what it pushes, deciding nothing."""
    pushed: list[tuple[ProposalScreen, Decided]] = []

    async def decide(proposal: ChecksProposed, decision: DecideProposal) -> None:
        pass

    return ProposalHost(lambda screen, decided: pushed.append((screen, decided)), decide), pushed


async def test_a_second_proposal_waits_until_the_first_is_decided() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        app.host.show(OTHER)
        app.host.show(OTHER)
        await pilot.pause()
        [first] = app.proposal_screens()
        assert first.proposal == PROPOSAL
        accept = DecideProposal(task_id="1", action="accept")
        first.dismiss(accept)
        await pilot.pause()
        [second] = app.proposal_screens()
        assert second.proposal == OTHER
        assert app.host.shown is second
        assert app.decisions == [(PROPOSAL, accept)]
        second.dismiss(None)
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.host.shown is None


async def test_withdrawing_the_shown_proposal_clears_it_before_its_screen_closes() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.host.withdraw(AgentExited(agent_id="agent-3", reason="finished"))
        assert app.host.shown is None
        app.host.follow(())
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.decisions == []


async def test_a_proposal_withdrawn_under_another_modal_is_closed_and_decides_nothing() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        other = ConfirmScreen("Other", "Another modal.", "yes", "no")
        app.push_screen(other)
        await pilot.pause()
        app.host.withdraw(AgentExited(agent_id="agent-3", reason="finished"))
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.host.shown is None
        assert app.screen is other
        assert app.decisions == []


async def test_withdrawing_the_shown_proposal_shows_the_next_waiting() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        app.host.show(OTHER)
        await pilot.pause()
        app.host.withdraw(AgentExited(agent_id="agent-3", reason="finished"))
        await pilot.pause()
        [screen] = app.proposal_screens()
        assert screen.proposal == OTHER
        assert app.host.shown is screen


async def test_an_agent_exiting_drops_its_waiting_proposal() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        app.host.show(OTHER)
        await pilot.pause()
        app.host.withdraw(AgentExited(agent_id="agent-4", reason="finished"))
        [first] = app.proposal_screens()
        first.dismiss(None)
        await pilot.pause()
        assert app.proposal_screens() == []
        assert app.host.shown is None


async def test_a_snapshot_replaces_the_waiting_proposals() -> None:
    third = PROPOSAL.model_copy(update={"agent_id": "agent-5", "task_id": "3"})
    host, pushed = recording_host()
    host.show(PROPOSAL)
    host.show(OTHER)
    host.follow((PROPOSAL, third, third))
    ((_, first_decided),) = pushed
    await first_decided(None)
    (_, _), (second, second_decided) = pushed
    assert second.proposal == third
    await second_decided(None)
    assert len(pushed) == 2
    assert host.shown is None


async def test_a_superseded_screens_late_decision_leaves_the_newer_tracked() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.host.follow((OTHER,))
        second = app.host.shown
        await pilot.pause()
        assert second is not None and second.proposal == OTHER
        assert app.host.shown is second
        assert app.proposal_screens() == [second]


async def test_a_superseded_screens_late_decision_sends_nothing() -> None:
    app = Host()
    callbacks = app.recording()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.host.follow((OTHER,))
        await pilot.pause()
        second = app.host.shown
        await callbacks[0](DecideProposal(task_id="1", action="accept"))
        await pilot.pause()
        assert app.decisions == []
        assert app.host.shown is second
        assert app.proposal_screens() == [second]


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


async def test_reopening_a_decided_proposal_shows_it_again() -> None:
    app = Host()
    async with app.run_test() as pilot:
        app.host.show(PROPOSAL)
        await pilot.pause()
        app.screen.dismiss(DecideProposal(task_id="1", action="accept"))
        await pilot.pause()
        app.host.reopen(PROPOSAL)
        await pilot.pause()
        [screen] = app.proposal_screens()
        assert screen.proposal == PROPOSAL
        assert app.host.shown is screen


async def test_reopening_while_another_is_shown_shows_it_next() -> None:
    third = PROPOSAL.model_copy(update={"agent_id": "agent-5", "task_id": "3"})
    host, pushed = recording_host()
    host.show(OTHER)
    host.show(third)
    host.show(PROPOSAL)
    host.reopen(PROPOSAL)
    host.reopen(OTHER)
    ((_, first_decided),) = pushed
    await first_decided(None)
    assert [screen.proposal for screen, _ in pushed] == [OTHER, PROPOSAL]
    await pushed[1][1](None)
    assert [screen.proposal for screen, _ in pushed] == [OTHER, PROPOSAL, third]
    await pushed[2][1](None)
    assert len(pushed) == 3


async def test_a_snapshot_forgets_the_agents_that_exited() -> None:
    host, pushed = recording_host()
    host.withdraw(AgentExited(agent_id="agent-3", reason="finished"))
    host.reopen(PROPOSAL)
    assert pushed == []
    host.follow(())
    host.reopen(PROPOSAL)
    [(screen, _)] = pushed
    assert screen.proposal == PROPOSAL
