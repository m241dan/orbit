"""Refusals of a name that isn't among the choices, naming the choices, and what's said of
errors: why each happened, and the reports of those nothing expected."""

from orbit.agent.domain.refusals import Refused, failed_to, reason, stopped_on, unknown
from orbit.protocol import BUSY, UNKNOWN


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


def test_an_errors_reason_is_what_it_says() -> None:
    assert reason(ValueError("a bug")) == "a bug"


def test_a_refusals_reason_is_its_message() -> None:
    assert reason(Refused(BUSY, "A turn is running.")) == "A turn is running."


def test_an_error_that_says_nothing_is_named_by_its_type() -> None:
    assert reason(RuntimeError()) == "RuntimeError"


def test_failing_to_do_something_says_orbit_failed_to_and_why() -> None:
    assert failed_to("handle RunTasks", ValueError("a bug")) == (
        "Orbit failed to handle RunTasks: a bug"
    )


def test_failing_on_an_error_that_says_nothing_names_its_type() -> None:
    assert failed_to("fetch the usage", KeyError()) == "Orbit failed to fetch the usage: KeyError"


def test_stopping_on_an_error_says_what_stopped_and_why() -> None:
    assert stopped_on("The work lane", ValueError("boom")) == (
        "The work lane stopped on an unexpected error: boom"
    )


def test_stopping_on_an_error_that_says_nothing_names_its_type() -> None:
    assert stopped_on("The event log writer", RuntimeError("")) == (
        "The event log writer stopped on an unexpected error: RuntimeError"
    )
