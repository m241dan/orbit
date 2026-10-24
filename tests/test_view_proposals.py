from orbit.protocol import ChecksProposed, DecideProposal, ProposalDecided, ProposedStep
from orbit.tui.view.proposals import (
    FEEDBACK_HINT,
    FEEDBACK_PLACEHOLDER,
    PROPOSAL_KEYS_HINT,
    decision_entry,
    proposal_title,
    revision,
)

PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="7", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)


def test_proposal_title_names_the_proposing_agent() -> None:
    assert proposal_title("agent-3") == "Proposed checks · agent-3"


def test_proposal_hints_name_the_keys() -> None:
    assert PROPOSAL_KEYS_HINT == "a accept · r revise · x end"
    assert FEEDBACK_HINT == "enter send · esc back"


def test_feedback_box_asks_what_should_change() -> None:
    assert FEEDBACK_PLACEHOLDER == "What should change?"


def test_feedback_asks_for_its_changes_trimmed() -> None:
    assert revision(PROPOSAL, "  add mypy ") == DecideProposal(
        task_id="7", action="revise", feedback="add mypy"
    )


def test_blank_feedback_asks_for_nothing() -> None:
    assert revision(PROPOSAL, " ") is None
    assert revision(PROPOSAL, "") is None


def test_a_decision_is_noted_as_orbit_telling_the_agent() -> None:
    accept = DecideProposal(task_id="7", action="accept")
    end = DecideProposal(task_id="7", action="end")
    revise = DecideProposal(task_id="7", action="revise", feedback="add mypy")
    assert decision_entry("agent-3", accept) == ("orbit → agent-3: proposal accepted", "agent")
    assert decision_entry("agent-3", end) == ("orbit → agent-3: task ended", "agent")
    assert decision_entry("agent-3", revise) == (
        "orbit → agent-3: changes asked for: add mypy",
        "agent",
    )


def test_another_frontends_decision_is_noted_the_same_way() -> None:
    decided = ProposalDecided(
        command_id="c", agent_id="agent-3", task_id="7", action="revise", feedback="More."
    )
    assert decision_entry("agent-3", decided) == (
        "orbit → agent-3: changes asked for: More.",
        "agent",
    )
