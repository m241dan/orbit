"""Which triggers fire on an event: those enabled, on it, for an enabled role or running the
checks on ``session_started``, whose ``when`` takes it and that didn't queue the task causing it,
the guards that hold one back, the landings counted toward the ``commits`` triggers, which fire
by hand, and what ``session_started`` says of verification."""

from collections.abc import Mapping

import pytest

from orbit.agent.domain.reviews import ReviewJob
from orbit.agent.domain.triggers import (
    Trigger,
    TriggerEvent,
    count_landing,
    fires_by_hand,
    held_back,
    listening,
    restart_counts,
    takes,
    verification_state,
)
from orbit.agent.domain.verification import VerificationStep
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


def test_a_trigger_running_the_checks_listens_only_on_session_started() -> None:
    started = TriggerEvent("session_started", {"verification": "configured"})
    gates = {
        "landed": Trigger(on="task_landed", checks="gate"),
        "started": Trigger(on="session_started", checks="gate"),
        "failed": Trigger(on="startup_check_failed", checks="gate"),
    }
    assert listening(gates, LANDED, ROLES, None) == []
    assert names(listening(gates, started, ROLES, None)) == ["started"]
    failed = TriggerEvent("startup_check_failed", {"step": "Lint"})
    assert listening(gates, failed, ROLES, None) == []


def test_a_trigger_running_the_checks_listens_whatever_roles_are_enabled() -> None:
    started = TriggerEvent("session_started", {"verification": "configured"})
    gate = Trigger(on="session_started", checks="gate", when={"verification": "configured"})
    assert names(listening({"gate": gate}, started, (), None)) == ["gate"]


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
    guarded = trigger(guards=("review-rounds",))
    assert not held_back(guarded, "Look at abc", [task("Look at abc", origin="review")])


@pytest.mark.parametrize("origin", ["review", "super-review"])
def test_suggestions_pending_holds_back_while_a_task_the_reviews_queued_waits(origin: str) -> None:
    guarded = trigger(guards=("suggestions-pending",))
    assert held_back(guarded, "Look at abc", [task("Tidy", "coder", origin=origin)])
    assert held_back(guarded, "Look at abc", [task("Review", status="running", origin=origin)])
    assert not held_back(guarded, "Look at abc", [task("Tidy", status="completed", origin=origin)])


def test_suggestions_pending_does_not_hold_back_for_the_users_or_another_triggers_tasks() -> None:
    guarded = trigger(guards=("suggestions-pending",))
    waiting = [
        task("Tidy", "coder"),
        task("Plan", "coder", origin="planner"),
        task("Look", origin="look"),
    ]
    assert not held_back(guarded, "Look at abc", waiting)


def test_dedupe_holds_nothing_back_before_there_is_a_prompt() -> None:
    guarded = trigger(guards=("dedupe",))
    assert not held_back(guarded, None, [task("Look at abc")])
    assert held_back(trigger(guards=("dedupe", "not-queued")), None, [task("Look at abc")])


# Counting landings toward commits triggers


def every(count: int | None, **fields: object) -> Trigger:
    """A trigger on ``commits`` queuing a super reviewer's task every ``count`` landings of the
    user's and the planner's tasks, with ``fields`` over that."""
    return Trigger.model_validate(
        {
            "on": "commits",
            "role": "super-reviewer",
            "prompt": "Review the codebase at {commit}",
            "when": {"origin": ["manual", "planner"]},
            "every": count,
            **fields,
        }
    )


def landing(origin: str = "manual", role: str = "coder") -> TriggerEvent:
    """A landing of ``role``'s task from ``origin``, raised on ``commits``."""
    return TriggerEvent(
        "commits", {"role": role, "origin": origin}, task("Add", role, origin=origin)
    )


def counted_over(
    triggers: dict[str, Trigger],
    events: list[TriggerEvent],
    tasks: tuple[TaskState, ...] = (),
    counts: Mapping[str, int] | None = None,
) -> tuple[list[list[str]], Mapping[str, int]]:
    """The names of the triggers that fire on each of ``events``, landings counted in turn from
    ``counts`` with ``tasks`` on the board, and the counts after the last."""
    fired: list[list[str]] = []
    now: Mapping[str, int] = counts or {}
    for event in events:
        counted = count_landing(triggers, event, (*ROLES, "super-reviewer"), now, tasks)
        fired.append(names(counted.fires))
        now = counted.counts
    return fired, now


def test_a_commits_trigger_fires_every_so_many_landings_its_count_starting_again() -> None:
    fired, counts = counted_over({"super": every(3)}, [landing()] * 7)
    assert fired == [[], [], ["super"], [], [], ["super"], []]
    assert counts == {"super": 1}


def test_landings_from_the_planner_count_too() -> None:
    fired, _ = counted_over({"super": every(2)}, [landing("planner"), landing("manual")])
    assert fired == [[], ["super"]]


def test_landings_its_when_does_not_take_do_not_count() -> None:
    events = [landing("review"), landing("super-review"), landing("manual", "reviewer")]
    fired, counts = counted_over(
        {"super": every(1, when={"origin": "manual", "role": "coder"})}, events
    )
    assert fired == [[], [], []]
    assert counts == {}


def test_a_trigger_does_not_count_the_landings_of_tasks_it_queued() -> None:
    fired, counts = counted_over({"super": every(1, when={})}, [landing("super")])
    assert (fired, counts) == ([[]], {})


def test_a_disabled_commits_trigger_or_one_whose_role_is_disabled_does_not_count() -> None:
    triggers = {"off": every(1, enabled=False), "tidy": every(1, role="tidier")}
    assert counted_over(triggers, [landing()]) == ([[]], {})


def test_a_due_trigger_a_guard_holds_back_stays_due_and_fires_once_none_does() -> None:
    triggers = {"super": every(2, guards=("suggestions-pending",))}
    waiting = (task("Tidy", "coder", origin="review"),)
    fired, counts = counted_over(triggers, [landing()] * 3, waiting)
    assert (fired, counts) == ([[], [], []], {"super": 3})
    fired, counts = counted_over(triggers, [landing()], counts=counts)
    assert (fired, counts) == ([["super"]], {"super": 0})


def test_each_commits_trigger_counts_apart_and_fires_in_its_order() -> None:
    triggers = {"often": every(1), "rarely": every(2), "listening": trigger()}
    fired, counts = counted_over(triggers, [landing()] * 2)
    assert fired == [["often"], ["often", "rarely"]]
    assert counts == {"often": 0, "rarely": 0}


def test_a_commits_trigger_without_every_fires_on_each_landing_it_counts() -> None:
    fired, _ = counted_over({"each": every(None)}, [landing()] * 2)
    assert fired == [["each"], ["each"]]


def test_counting_keeps_the_counts_of_the_triggers_that_did_not_hear_it() -> None:
    _, counts = counted_over({"super": every(5)}, [landing()], counts={"gone": 4})
    assert counts == {"gone": 4, "super": 1}


def test_restarting_counts_sets_only_the_triggers_fired_back_to_nothing() -> None:
    assert restart_counts({"a": 3, "b": 2}, ["a", "c"]) == {"a": 0, "b": 2, "c": 0}


def test_a_trigger_fires_by_hand_if_it_and_its_role_are_enabled_whatever_its_when() -> None:
    roles = ("super-reviewer",)
    assert fires_by_hand(
        every(10, when={"origin": "nobody"}, guards=("suggestions-pending",)), roles
    )
    assert not fires_by_hand(every(10, enabled=False), roles)
    assert not fires_by_hand(every(10), ("coder",))


# What session_started says of verification


@pytest.mark.parametrize(
    ("steps", "state"),
    [
        (None, "off"),
        ((), "missing"),
        ((VerificationStep("Lint", ("ruff", "check")),), "configured"),
    ],
)
def test_verification_is_configured_with_a_step_missing_with_none_and_off_without_it(
    steps: tuple[VerificationStep, ...] | None, state: str
) -> None:
    assert verification_state(steps) == state
