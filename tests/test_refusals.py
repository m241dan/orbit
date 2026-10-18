"""Refusals of a name that isn't among the choices, naming the choices."""

from orbit.agent.domain.refusals import unknown
from orbit.protocol import UNKNOWN


def test_an_unknown_name_is_refused_as_unknown_with_the_problem_and_the_choices() -> None:
    refused = unknown("No lane 'x'", "the lanes are", ("work", "planner"))

    assert (refused.code, refused.message) == (
        UNKNOWN,
        "No lane 'x'; the lanes are work and planner.",
    )


def test_a_single_choice_is_named_alone() -> None:
    refused = unknown("No agent 'x'", "the agent is", ("planner",))

    assert refused.message == "No agent 'x'; the agent is planner."


def test_three_choices_are_named_in_order_with_and_before_the_last() -> None:
    refused = unknown("No lane 'x'", "the lanes are", ("a", "b", "c"))

    assert refused.message == "No lane 'x'; the lanes are a, b and c."
