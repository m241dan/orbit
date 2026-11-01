"""Which triggers fire on an event: those enabled, on it, for an enabled role, whose ``when``
takes it and that didn't queue the task causing it, and the guards that hold one back."""

import pytest

from orbit.agent.domain.reviews import ReviewJob
from orbit.agent.domain.triggers import Trigger, TriggerEvent, held_back, listening, takes
from orbit.protocol import TaskSpec, TaskState, TaskStatus

ROLES = ("coder", "reviewer")
LANDED = TriggerEvent("task_landed", {"role": "coder", "origin": "manual"})
REVIEW = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))


def trigger(**fields: object) -> Trigger:
    """A trigger on ``task_landed`` queuing a reviewer's task, with ``fields`` over that."""
    return Trigger.model_validate(
        {"on": "task_landed", "role": "reviewer", "prompt": "Look at {commit}", **fields}
    )


def task(
    prompt: str, role: str = "reviewer", status: TaskStatus = "pending", origin: str = "manual"
) -> TaskState:
    """A task on the board: ``role``'s, with ``prompt``, as ``status``, queued by ``origin``."""
    return TaskState(id="9", task=TaskSpec(prompt=prompt, role=role), status=status, origin=origin)


def names(found: list[tuple[str, Trigger]]) -> list[str]:
    return [name for name, _ in found]


# Listening


def test_the_triggers_on_an_event_listen_in_their_order() -> None:
    triggers = {"b": trigger(), "other": trigger(on="task_finished"), "a": trigger()}
    assert names(listening(triggers, LANDED, ROLES, None)) == ["b", "a"]


def test_a_disabled_trigger_does_not_listen() -> None:
    triggers = {"off": trigger(enabled=False), "on": trigger()}
    assert names(listening(triggers, LANDED, ROLES, None)) == ["on"]


def test_a_trigger_whose_role_is_not_enabled_does_not_listen() -> None:
    triggers = {"tidy": trigger(role="tidier"), "look": trigger()}
    assert names(listening(triggers, LANDED, ROLES, None)) == ["look"]


def test_a_trigger_running_the_checks_does_not_listen_yet() -> None:
    gate = Trigger(on="task_landed", checks="gate")
    assert listening({"gate": gate}, LANDED, ROLES, None) == []


def test_a_trigger_does_not_listen_to_an_event_a_task_it_queued_caused() -> None:
    caused = TriggerEvent("task_landed", {"role": "coder"}, task("Look at abc", origin="look"))
    triggers = {"look": trigger(), "other": trigger()}
    assert names(listening(triggers, caused, ROLES, None)) == ["other"]


def test_review_rounds_holds_a_trigger_back_unless_a_review_is_due() -> None:
    triggers = {"review": trigger(guards=("review-rounds",)), "look": trigger()}
    assert names(listening(triggers, LANDED, ROLES, None)) == ["look"]
    assert names(listening(triggers, LANDED, ROLES, REVIEW)) == ["review", "look"]


@pytest.mark.parametrize(
    ("when", "taken"),
    [
        ({}, True),
        ({"role": "coder"}, True),
        ({"role": "reviewer"}, False),
        ({"origin": ["manual", "planner"]}, True),
        ({"origin": ["review"]}, False),
        ({"role": "coder", "origin": "review"}, False),
        ({"status": "completed"}, False),
    ],
)
def test_when_takes_an_event_whose_fields_each_match_its_value_or_one_of_its_values(
    when: dict[str, object], taken: bool
) -> None:
    assert takes(trigger(when=when).when, LANDED.fields) is taken


# Guards held on the board


def test_not_queued_holds_back_while_a_task_of_its_role_waits() -> None:
    guarded = trigger(guards=("not-queued",))
    assert held_back(guarded, "Look at abc", [task("Other", status="running")])
    assert held_back(guarded, "Look at abc", [task("Other")])
    assert not held_back(guarded, "Look at abc", [task("Other", status="completed")])
    assert not held_back(guarded, "Look at abc", [task("Other", role="coder")])


def test_dedupe_holds_back_a_task_whose_first_line_a_waiting_task_has() -> None:
    guarded = trigger(guards=("dedupe",))
    assert held_back(guarded, "Look at abc\n\nMore.", [task("  Look at abc\n\nOther.", "coder")])
    assert not held_back(guarded, "Look at abc", [task("Look at abc", status="gave_up")])
    assert not held_back(guarded, "Look at abc", [task("Look at abd")])


def test_without_guards_held_on_the_board_nothing_holds_a_trigger_back() -> None:
    guarded = trigger(guards=("review-rounds", "suggestions-pending"))
    assert not held_back(guarded, "Look at abc", [task("Look at abc")])
