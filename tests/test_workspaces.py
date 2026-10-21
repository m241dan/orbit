"""Which served workspace a command goes to, and the refusals of those it can't."""

import pytest

from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.workspaces import ATTACH_FIRST, addressed_workspace, served_workspace
from orbit.protocol import INVALID, UNKNOWN, WRONG_SESSION


def refusal(error: pytest.ExceptionInfo[Refused]) -> tuple[str, str]:
    return error.value.code, error.value.message


def test_a_served_workspace_is_found() -> None:
    assert served_workspace("b", ("a", "b")) == "b"


def test_a_workspace_not_served_is_unknown_naming_those_served() -> None:
    with pytest.raises(Refused) as error:
        served_workspace("c", ("a", "b"))

    assert refusal(error) == (UNKNOWN, "No workspace 'c'; the workspaces served are a and b.")


def test_the_one_workspace_served_is_named_alone() -> None:
    with pytest.raises(Refused) as error:
        served_workspace("c", ("a",))

    assert refusal(error) == (UNKNOWN, "No workspace 'c'; the workspace served is a.")


def test_no_workspace_is_known_while_none_is_served() -> None:
    with pytest.raises(Refused) as error:
        served_workspace("c", ())

    assert refusal(error) == (UNKNOWN, "No workspace 'c'; none is served.")


def test_a_command_naming_no_workspace_goes_to_the_one_attached() -> None:
    assert addressed_workspace(None, "a", ("a", "b")) == "a"


def test_a_command_naming_the_workspace_attached_goes_there() -> None:
    assert addressed_workspace("a", "a", ("a", "b")) == "a"


def test_a_command_before_attaching_is_invalid() -> None:
    with pytest.raises(Refused) as error:
        addressed_workspace(None, None, ("a",))

    assert refusal(error) == (INVALID, ATTACH_FIRST)


def test_a_command_for_another_served_workspace_is_for_the_wrong_session_naming_both() -> None:
    with pytest.raises(Refused) as error:
        addressed_workspace("b", "a", ("a", "b"))

    assert refusal(error) == (
        WRONG_SESSION,
        "Workspace 'b' isn't the one attached; this connection is attached to a.",
    )


def test_a_command_for_a_workspace_not_served_is_unknown_even_before_attaching() -> None:
    with pytest.raises(Refused) as error:
        addressed_workspace("c", None, ("a",))

    assert refusal(error)[0] == UNKNOWN
