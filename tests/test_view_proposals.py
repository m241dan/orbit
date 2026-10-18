from orbit.tui.view import (
    FEEDBACK_HINT,
    FEEDBACK_PLACEHOLDER,
    PROPOSAL_KEYS_HINT,
    proposal_title,
)


def test_proposal_title_names_the_proposing_agent() -> None:
    assert proposal_title("agent-3") == "Proposed checks · agent-3"


def test_proposal_hints_name_the_keys() -> None:
    assert PROPOSAL_KEYS_HINT == "a accept · r revise · x end"
    assert FEEDBACK_HINT == "enter send · esc back"


def test_feedback_box_asks_what_should_change() -> None:
    assert FEEDBACK_PLACEHOLDER == "What should change?"
