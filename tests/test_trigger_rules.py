"""Which triggers fire on an event: those enabled, on it, for an enabled role, running a named
set, or running the task checks on ``session_started``, whose ``when`` takes it and that didn't
queue the task causing it, the guards that hold one back, the landings counted toward the
``commits`` triggers, which fire by hand, and what ``session_started`` says of verification."""

from collections.abc import Mapping

import pytest

from orbit.agent.domain.reviews import ReviewJob
from orbit.agent.domain.triggers import (
    Trigger,
    TriggerEvent,
    can_queue_reviews,
    count_landing,
    fires_by_hand,
    held_back,
    listening,
    needs_paths,
    note_skipped_reviews,
    restart_counts,
    sift_changes,
    takes,
    verification_state,
    within_paths,
)
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import TaskSpec, TaskState, TaskStatus

ROLES = ("coder", "reviewer")
LANDED = TriggerEvent("task_landed", {"role": "coder", "origin": "manual", "review": "commit"})
REVIEW = ReviewJob(commits=("c1",), round=0, batch=None, reviewed=("1",))
BATCH_LANDED = TriggerEvent("task_landed", {"role": "coder", "origin": "review", "review": "batch"})
"""A batch that's done, whose review fires the triggers ``review-rounds`` guards."""
BATCH_REVIEW = ReviewJob(commits=("c2", "c3"), round=1, batch="batch-1", reviewed=("5", "6"))


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


# Whether reviews of commits can be queued

GUARDED = trigger(guards=["review-rounds"])
"""A trigger ``review-rounds`` guards, queuing a reviewer's task."""


def test_reviews_can_be_queued_while_an_enabled_trigger_review_rounds_guards_queues_a_task() -> (
    None
):
    assert can_queue_reviews({"plain": trigger(), "lens": GUARDED}, ROLES)


def test_reviews_cant_be_queued_without_a_trigger_review_rounds_guards() -> None:
    assert not can_queue_reviews({"plain": trigger(), "other": trigger(on="task_finished")}, ROLES)


def test_reviews_cant_be_queued_while_the_only_guarded_trigger_is_disabled() -> None:
    assert not can_queue_reviews({"lens": trigger(guards=["review-rounds"], enabled=False)}, ROLES)


def test_reviews_cant_be_queued_while_the_guarded_triggers_role_is_disabled() -> None:
    assert not can_queue_reviews({"lens": GUARDED}, ("coder",))


def test_reviews_can_be_queued_by_a_guarded_trigger_running_a_named_set() -> None:
    sets = {"lens": Trigger(on="task_landed", checks="mutation", guards=("review-rounds",))}
    assert can_queue_reviews(sets, ())


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


def test_a_trigger_running_the_task_checks_listens_only_on_session_started() -> None:
    started = TriggerEvent("session_started", {"verification": "configured"})
    checks = {
        "landed": Trigger(on="task_landed", checks="task-checks"),
        "started": Trigger(on="session_started", checks="task-checks"),
        "failed": Trigger(on="startup_check_failed", checks="task-checks"),
    }
    assert listening(checks, LANDED, ROLES, None) == []
    assert names(listening(checks, started, ROLES, None)) == ["started"]
    failed = TriggerEvent("startup_check_failed", {"step": "Lint"})
    assert listening(checks, failed, ROLES, None) == []


def test_a_trigger_running_the_task_checks_listens_whatever_roles_are_enabled() -> None:
    started = TriggerEvent("session_started", {"verification": "configured"})
    check = Trigger(on="session_started", checks="task-checks", when={"verification": "configured"})
    assert names(listening({"check": check}, started, (), None)) == ["check"]


def test_a_trigger_running_a_named_set_listens_on_any_event_whatever_roles_are_enabled() -> None:
    started = TriggerEvent("session_started", {"verification": "configured"})
    failed = TriggerEvent("startup_check_failed", {"step": "Lint"})
    sets = {
        "started": Trigger(on="session_started", checks="slow"),
        "landed": Trigger(on="task_landed", checks="slow", when={"role": "coder"}),
        "failed": Trigger(on="startup_check_failed", checks="slow"),
    }
    assert names(listening(sets, started, (), None)) == ["started"]
    assert names(listening(sets, LANDED, (), None)) == ["landed"]
    assert names(listening(sets, failed, (), None)) == ["failed"]


def test_a_trigger_does_not_listen_to_an_event_a_task_it_queued_caused() -> None:
    caused = TriggerEvent("task_landed", {"role": "coder"}, task("Look at abc", origin="look"))
    triggers = {"look": trigger(), "other": trigger()}
    assert names(listening(triggers, caused, ROLES, None)) == ["other"]


def test_review_rounds_holds_a_trigger_back_unless_a_review_is_due() -> None:
    triggers = {"review": trigger(guards=("review-rounds",)), "look": trigger()}
    assert names(listening(triggers, LANDED, ROLES, None)) == ["look"]
    assert names(listening(triggers, LANDED, ROLES, REVIEW)) == ["review", "look"]


RAISING = {
    "fit": trigger(role="fit-reviewer", guards=("review-rounds", "raised-finding")),
    "bugs": trigger(role="bugs-reviewer", guards=("review-rounds",)),
}
"""A lens ``raised-finding`` guards, and one it doesn't, each reviewing what's due."""
LENS_ROLES = ("fit-reviewer", "bugs-reviewer")


def test_raised_finding_holds_a_trigger_back_on_a_batch_review_its_role_raised_nothing_in() -> None:
    found = listening(RAISING, BATCH_LANDED, LENS_ROLES, BATCH_REVIEW, ("tests-reviewer",))
    assert names(found) == ["bugs"]


def test_raised_finding_lets_a_trigger_review_a_batch_its_role_raised_a_finding_in() -> None:
    found = listening(RAISING, BATCH_LANDED, LENS_ROLES, BATCH_REVIEW, ("fit-reviewer",))
    assert names(found) == ["fit", "bugs"]


def test_raised_finding_holds_nothing_back_on_a_commit_review() -> None:
    assert names(listening(RAISING, LANDED, LENS_ROLES, REVIEW)) == ["fit", "bugs"]


def test_raised_finding_holds_nothing_back_on_another_event_of_a_batch() -> None:
    finished = TriggerEvent("reviews_finished", {"findings": "some", "review": "batch"})
    merge = {"merge": trigger(on="reviews_finished", guards=("raised-finding",))}
    assert names(listening(merge, finished, ROLES, BATCH_REVIEW)) == ["merge"]


def test_a_trigger_taking_review_batch_listens_on_a_batch_review_only() -> None:
    triggers = {"fix": trigger(guards=("review-rounds",), when={"review": "batch"})}
    assert listening(triggers, LANDED, ROLES, REVIEW) == []
    assert names(listening(triggers, BATCH_LANDED, ROLES, BATCH_REVIEW)) == ["fix"]


def test_raised_finding_is_decided_as_a_trigger_listens_so_the_board_holds_nothing_back() -> None:
    assert not held_back(RAISING["fit"], "Look at c1", [task("Look at c1")])


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
        ({"review": "commit"}, True),
        ({"review": "batch"}, False),
    ],
)
def test_when_takes_an_event_whose_fields_each_match_its_value_or_one_of_its_values(
    when: dict[str, object], taken: bool
) -> None:
    assert takes(trigger(when=when).when, LANDED.fields) is taken


# Paths a landing changed


SECURITY = trigger(paths=("src/**/external/**", "pyproject.toml"))


def test_paths_are_needed_only_while_a_trigger_listening_has_them() -> None:
    assert needs_paths([("plain", trigger()), ("security", SECURITY)])
    assert not needs_paths([("plain", trigger())])


def test_a_trigger_with_paths_keeps_listening_only_if_a_changed_path_matches_one() -> None:
    found = [("plain", trigger()), ("security", SECURITY)]
    assert within_paths(found, ["src/orbit/agent/external/git.py"]) == found
    assert within_paths(found, ["README.md", "pyproject.toml"]) == found
    assert within_paths(found, ["README.md"]) == [("plain", trigger())]
    assert within_paths(found, []) == [("plain", trigger())]


# code-changed, decided by the paths a landing changed

CODE_GUARDED = trigger(role="bugs-reviewer", guards=("code-changed",))
"""A lens ``code-changed`` guards, without ``paths`` of its own."""


def test_changed_paths_are_needed_while_a_trigger_listening_has_code_changed() -> None:
    assert needs_paths([("plain", trigger()), ("bugs", CODE_GUARDED)])


@pytest.mark.parametrize(
    ("changed", "fired", "held"),
    [
        (["docs/x.md"], ["plain"], ["bugs"]),
        (["src/x.md"], ["plain", "bugs"], []),
        (["scripts/x.py"], ["plain", "bugs"], []),
        (["Makefile"], ["plain", "bugs"], []),
    ],
)
def test_code_changed_holds_back_its_trigger_when_nothing_changed_is_code(
    changed: list[str], fired: list[str], held: list[str]
) -> None:
    sifted = sift_changes([("plain", trigger()), ("bugs", CODE_GUARDED)], changed, ["src"])
    assert (names(sifted.fire), names(sifted.held)) == (fired, held)


def test_a_trigger_with_paths_is_decided_by_them_alone_whatever_code_changed_says() -> None:
    docs = CODE_GUARDED.model_copy(update={"paths": ("docs/**",)})
    assert names(sift_changes([("docs", docs)], ["docs/x.md"], []).fire) == ["docs"]
    assert sift_changes([("docs", docs)], ["src/x.py"], []) == sift_changes([], [], [])


def test_code_changed_holds_nothing_the_paths_held_already() -> None:
    narrowed = CODE_GUARDED.model_copy(update={"paths": ("src/**",)})
    sifted = sift_changes([("bugs", narrowed), ("plain", trigger())], ["docs/x.md"], [])
    assert (names(sifted.fire), sifted.held) == (["plain"], [])


@pytest.mark.parametrize(
    ("roles", "prompts", "note"),
    [
        (
            [["bugs-reviewer"]],
            ["Fix the parser\n\nMore."],
            'Skipped the bugs reviews of "Fix the parser": it changed no code.',
        ),
        (
            [["license-check"]],
            ["Fix it"],
            'Skipped the license-check reviews of "Fix it": it changed no code.',
        ),
        (
            [["bugs-reviewer", "tests-reviewer"], ["logic-reviewer"], ["bugs-reviewer"]],
            ["Fix the parser", "Two", "Three"],
            'Skipped the bugs, tests and logic reviews of "Fix the parser" and 2 more:'
            " it changed no code.",
        ),
        ([], ["Fix it"], None),
        ([["bugs-reviewer"]], [], None),
    ],
)
def test_the_note_of_skipped_reviews_names_the_lenses_and_the_first_task(
    roles: list[list[str]], prompts: list[str], note: str | None
) -> None:
    held = [(str(index), trigger(role=role)) for index, role in enumerate(roles)]
    assert note_skipped_reviews(held, prompts) == note


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


def suggested(status: TaskStatus = "pending", origin: str = "review") -> TaskState:
    """A coder's task a review queued by ``origin`` suggested, in its batch, as ``status``."""
    return task("Tidy", "coder", status, origin).model_copy(update={"batch": "batch-1", "round": 1})


def booked(
    role: str = "reviewer", status: TaskStatus = "pending", origin: str = "review"
) -> TaskState:
    """``role``'s task the trigger ``origin`` queued as a review, booked into the review lane, as
    ``status``."""
    return task("Review", role, status, origin).model_copy(update={"queued_as": "review"})


@pytest.mark.parametrize("origin", ["review", "super-review", "audit"])
def test_suggestions_pending_holds_back_while_a_task_the_reviews_queued_waits(origin: str) -> None:
    guarded = trigger(guards=("suggestions-pending",))
    assert held_back(guarded, "Look at abc", [suggested(origin=origin)])
    assert held_back(guarded, "Look at abc", [suggested("running", origin)])
    assert held_back(guarded, "Look at abc", [booked(status="running", origin=origin)])
    assert held_back(guarded, "Look at abc", [booked("super-reviewer", origin=origin)])
    assert not held_back(guarded, "Look at abc", [suggested("completed", origin)])
    assert not held_back(guarded, "Look at abc", [booked(status="error", origin=origin)])


@pytest.mark.parametrize("role", ["coder", "auditor"])
@pytest.mark.parametrize("status", ["pending", "running"])
def test_suggestions_pending_holds_back_while_another_roles_review_waits(
    role: str, status: TaskStatus
) -> None:
    guarded = trigger(guards=("suggestions-pending",))
    assert held_back(guarded, "Look at abc", [booked(role, status, "audit")])


def test_suggestions_pending_does_not_hold_back_for_the_users_or_another_triggers_tasks() -> None:
    guarded = trigger(guards=("suggestions-pending",))
    waiting = [
        task("Tidy", "coder"),
        task("Plan", "coder", origin="planner"),
        task("Look"),
        task("Check", origin="planner"),
        task("Fix the lint step", "coder", origin="startup-fix"),
    ]
    assert not held_back(guarded, "Look at abc", waiting)


@pytest.mark.parametrize("role", ["reviewer", "super-reviewer"])
@pytest.mark.parametrize("status", ["pending", "running"])
def test_suggestions_pending_does_not_hold_back_for_a_review_a_trigger_did_not_book(
    role: str, status: TaskStatus
) -> None:
    guarded = trigger(guards=("suggestions-pending",))
    assert not held_back(guarded, "Look at abc", [task("Look", role, status, "look")])


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
    fired, counts = counted_over(triggers, [landing()] * 3, (suggested(),))
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


def test_a_set_fires_by_hand_if_its_trigger_is_enabled_but_the_task_checks_do_not() -> None:
    assert fires_by_hand(Trigger(on="manual", checks="slow", when={}), ())
    assert not fires_by_hand(Trigger(on="manual", checks="slow", enabled=False), ())
    assert not fires_by_hand(Trigger(on="manual", checks="task-checks"), ROLES)


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
