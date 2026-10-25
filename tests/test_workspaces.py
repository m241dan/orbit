"""Which served workspace a command goes to, and the refusals of those it can't."""

import pytest

from orbit.agent.domain.activity import Activity, switch_refusal
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.workspaces import (
    ATTACH_FIRST,
    PathKind,
    addressed_workspace,
    check_addable,
    check_directory,
    check_removable,
    served_workspace,
)
from orbit.protocol import BUSY, INVALID, PLANNER, UNKNOWN, WRONG_SESSION


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


def test_an_absolute_path_may_be_added() -> None:
    check_addable("/src/c", absolute=True)


def test_a_relative_path_is_invalid() -> None:
    with pytest.raises(Refused) as error:
        check_addable("src/c", absolute=False)

    assert refusal(error) == (INVALID, "Can't add src/c: give the absolute path to a directory.")


def test_a_directory_may_be_added() -> None:
    check_directory("/src/c", PathKind.DIRECTORY)


def test_a_path_with_nothing_there_is_unknown() -> None:
    with pytest.raises(Refused) as error:
        check_directory("/src/c", PathKind.MISSING)

    assert refusal(error) == (UNKNOWN, "Can't add /src/c: there's nothing there.")


def test_a_file_is_invalid() -> None:
    with pytest.raises(Refused) as error:
        check_directory("/src/c", PathKind.FILE)

    assert refusal(error) == (INVALID, "Can't add /src/c: it isn't a directory.")


def test_the_startup_check_alone_leaves_a_workspace_idle_for_switching_and_removal() -> None:
    activity = Activity(startup_check=True)

    check_removable("/src/c", activity, ())
    assert (switch_refusal(activity), activity.running) == (None, False)


@pytest.mark.parametrize(
    ("activity", "work"),
    [
        (Activity(tasks=True), "tasks are running"),
        (Activity(review=True), "a review is running"),
        (Activity(answering=(PLANNER,)), "the planner is answering"),
        (Activity(tasks=True, review=True, answering=(PLANNER,)), "tasks are running"),
        (Activity(tasks=True, answering=(PLANNER,)), "tasks are running"),
        (Activity(review=True, answering=(PLANNER,)), "a review is running"),
        (Activity(answering=("architect",)), "the architect is answering"),
    ],
)
def test_a_workspace_running_work_is_busy_naming_it(activity: Activity, work: str) -> None:
    with pytest.raises(Refused) as error:
        check_removable("/src/c", activity, ("c-1",))

    assert refusal(error) == (BUSY, f"Can't remove /src/c: {work}.")


def test_a_workspace_another_frontend_views_is_busy_naming_the_session() -> None:
    with pytest.raises(Refused) as error:
        check_removable("/src/c", Activity(), ("c-1",))

    assert refusal(error) == (BUSY, "Can't remove /src/c: another frontend views its session c-1.")


def test_a_workspace_others_view_is_busy_naming_each_session() -> None:
    with pytest.raises(Refused) as error:
        check_removable("/src/c", Activity(), ("c-1", "c-2"))

    message = "Can't remove /src/c: another frontend views its sessions c-1 and c-2."
    assert refusal(error) == (BUSY, message)
