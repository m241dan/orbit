from textual.widgets import Input, Static

from orbit.protocol import (
    AgentExited,
    AgentStarted,
    ChecksProposed,
    DecideProposal,
    ProposedSetup,
    ProposedStep,
)
from orbit.tui import OrbitApp
from orbit.tui.proposals import ProposalScreen
from orbit.tui.transcript import Transcript
from orbit.tui.view.proposals import proposal_text
from tests.test_task_list import feed
from tests.test_tui import FakeSession

PROPOSAL = ChecksProposed(
    agent_id="agent-3",
    task_id="7",
    steps=(
        ProposedStep(name="Lint", command=("ruff", "check")),
        ProposedStep(name="Tests", command=("uv", "run", "pytest", "-k", "not slow")),
    ),
    setup=ProposedSetup(command=("uv", "sync"), watch=("uv.lock", "pyproject.toml")),
)
STARTED = AgentStarted(agent_id="agent-3", role="configurator", task_id="7")


def entries(app: OrbitApp) -> list[str]:
    return [entry.text for entry in app.query_one(Transcript).entries(None)]


def test_the_proposal_reads_setup_then_each_step_and_that_they_passed() -> None:
    assert proposal_text(PROPOSAL) == (
        "Setup: uv sync\n"
        "  reruns when these change: uv.lock, pyproject.toml\n"
        "\n"
        "Steps:\n"
        "  1. Lint: ruff check\n"
        "  2. Tests: uv run pytest -k 'not slow'\n"
        "\n"
        "✅ All passed on the current code."
    )
    bare = PROPOSAL.model_copy(
        update={"setup": ProposedSetup(command=("make",)), "steps": PROPOSAL.steps[:1]}
    )
    assert proposal_text(bare).startswith("Setup: make\n\nSteps:\n  1. Lint: ruff check\n")
    unset = PROPOSAL.model_copy(update={"setup": None})
    assert proposal_text(unset).startswith("Setup: none\n\nSteps:")


async def test_a_proposal_opens_a_modal_showing_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        screen = app.screen
        assert isinstance(screen, ProposalScreen)
        body = screen.query_one("#proposal-body", Static)
        assert str(body.render()) == proposal_text(PROPOSAL)
        assert not screen.query_one(Input).display
        assert session.commands == []


async def test_a_accepts_and_notes_it_in_the_conversation() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("a")
        await pilot.pause()
        assert session.commands == [DecideProposal(task_id="7", action="accept")]
        assert not isinstance(app.screen, ProposalScreen)
        assert entries(app)[-1] == "orbit → agent-3: proposal accepted"
        # Under its role's tab, too.
        assert app.query_one(Transcript).entries("configurator")[-1].text == entries(app)[-1]


async def test_x_ends_the_task() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("x")
        await pilot.pause()
        assert session.commands == [DecideProposal(task_id="7", action="end")]
        assert entries(app)[-1] == "orbit → agent-3: task ended"


async def test_r_takes_feedback_and_enter_sends_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("r")
        feedback = app.screen.query_one(Input)
        assert feedback.display and feedback.has_focus
        # Keys type into the feedback rather than deciding.
        await pilot.press(*"add mypy", "enter")
        await pilot.pause()
        assert session.commands == [
            DecideProposal(task_id="7", action="revise", feedback="add mypy")
        ]
        assert entries(app)[-1] == "orbit → agent-3: changes asked for: add mypy"


async def test_blank_feedback_is_not_sent() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("r", "space", "enter")
        await pilot.pause()
        assert session.commands == []
        assert isinstance(app.screen, ProposalScreen)


async def test_esc_from_the_feedback_goes_back_to_the_proposal() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("r", *"hm", "escape")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ProposalScreen)
        assert not screen.query_one(Input).display
        assert session.commands == []
        # Back at the proposal, its keys work again.
        await pilot.press("a")
        await pilot.pause()
        assert session.commands == [DecideProposal(task_id="7", action="accept")]


async def test_esc_on_the_proposal_does_not_cancel_or_close_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ProposalScreen)
        assert session.commands == []


async def test_the_proposal_closes_when_its_agent_exits_or_is_cancelled() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        other = AgentExited(agent_id="agent-9", reason="finished")
        await feed(session, pilot, STARTED, PROPOSAL, other)
        assert isinstance(app.screen, ProposalScreen)
        await feed(session, pilot, AgentExited(agent_id="agent-3", reason="killed"))
        assert not isinstance(app.screen, ProposalScreen)
        assert session.commands == []
        # Another agent exiting later finds no proposal to close.
        await feed(session, pilot, other)
        assert not isinstance(app.screen, ProposalScreen)


async def test_a_proposal_shown_while_typing_takes_over_the_keys() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await pilot.press("i")
        await feed(session, pilot, STARTED, PROPOSAL)
        await pilot.press("x")
        await pilot.pause()
        assert session.commands == [DecideProposal(task_id="7", action="end")]
