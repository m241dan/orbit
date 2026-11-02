"""``set_finished`` in a session: a named set's run ending, passed or failed, fires the triggers
on it; a failing reporting set's notice given unless they queued a task; the tasks of finding
roles it queues reviewed as one report of the set, fanning in to a merge pass with
``review = "set"``; any other role's an ordinary task, with reviews on or off; and the startup
check raising nothing."""

import asyncio
from typing import Any

from orbit.agent import Verification, VerificationStep, WorkspaceHost
from orbit.agent.domain.triggers import Trigger
from orbit.agent.domain.verification import CheckSet
from orbit.protocol import (
    CODER,
    REVIEWER,
    TESTS_REVIEWER,
    Event,
    Notice,
    StartupChecked,
    TasksChanged,
    TaskState,
    TaskStatus,
    TurnEndReason,
)
from tests.agent_fakes import (
    LINT,
    TRIGGERS,
    Call,
    FakeTree,
    Script,
    Steps,
    agent_exited,
    board,
    claims_done,
    events_until,
    files,
    reports,
    session_for,
    settle,
    settled,
    start,
)
from tests.waiting import eventually

MUTATION = VerificationStep("Mutation tests", ("mutmut", "run"), "mutation")
MUTATION_SET = CheckSet("mutation", (MUTATION,))

RUN_MUTATION = Trigger.model_validate(
    {"on": "task_landed", "checks": "mutation", "when": {"role": CODER, "origin": "manual"}}
)
"""A project's trigger running the set mutation as each of the user's tasks lands."""

MUTATION_REPORT = Trigger(
    on="set_finished",
    role=TESTS_REVIEWER,
    prompt="Look into {set}, {status} at {commit}.\n\n{output}",
    when={"set": "mutation", "status": "failed"},
)
"""A project's trigger routing a failing mutation run to the tests lens."""

SET_MERGE = Trigger(
    on="reviews_finished",
    role=REVIEWER,
    prompt="{title}\n\n{findings}\n\nAt most {cap}.",
    when={"findings": "some", "review": "set"},
)
"""A project's merge pass, only on the review of a set's report that found something."""

OFF = ("review", "merge-pass")
"""The triggers the tests disable: the single reviewer's and the shipped merge pass."""


def mutation_triggers(**more: Trigger) -> dict[str, Trigger]:
    """The triggers of :data:`~tests.agent_fakes.TRIGGERS` with :data:`OFF` disabled, the set
    mutation run as the user's tasks land, and ``more``."""
    off = {name: TRIGGERS[name].model_copy(update={"enabled": False}) for name in OFF}
    return {**TRIGGERS, **off, "run-mutation": RUN_MUTATION} | more


def with_mutation(steps: Steps) -> Verification:
    """Verification with the task check Lint and the set mutation, played by ``steps``."""
    return Verification((LINT,), steps, sets=(MUTATION_SET,))


def reporting(
    steps: Steps, *scripts: Script, reviewers: tuple[Script, ...] = (), **options: Any
) -> WorkspaceHost:
    """A session landing work in a tree, running mutation as it lands, played by ``steps``,
    its tasks by ``scripts`` and its merge passes by ``reviewers``; ``options`` go to
    :func:`session_for`, the triggers of :func:`mutation_triggers` and the verification of
    :func:`with_mutation` unless they give others."""
    defaults: dict[str, Any] = {
        "triggers": mutation_triggers(),
        "review": True,
        "verification": with_mutation(steps),
    }
    session, _, _ = session_for(
        *scripts, reviewers=reviewers, tree=FakeTree(), **(defaults | options)
    )
    return session


async def run_until(session: WorkspaceHost, *statuses: TaskStatus) -> list[Event]:
    """The events of ``session`` once the user's task ran and mutation's run ended, and the
    board's tasks then stand at ``statuses``; the session is closed after."""
    exited = agent_exited("set-mutation")

    def done(events: list[Event]) -> bool:
        ran = any(exited(event) for event in events)
        return ran and _standing(events, statuses)

    await start(session)
    events = await asyncio.wait_for(settle(session, until=done), 10)
    await session.close()
    return events


def _standing(events: list[Event], statuses: tuple[TaskStatus, ...]) -> bool:
    """Whether the last board among ``events`` has its tasks at ``statuses``."""
    if not any(isinstance(event, TasksChanged) for event in events):
        return False
    return tuple(t.status for t in board(events)) == statuses


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str | None, int, TaskStatus]]:
    """Each of ``tasks``' role, origin, batch, round and status."""
    return [(t.task.role, t.origin, t.batch, t.round, t.status) for t in tasks]


def notices(events: list[Event]) -> list[str]:
    return [event.message for event in events if isinstance(event, Notice)]


async def test_a_failing_mutation_run_goes_to_the_tests_lens_and_one_merge_pass() -> None:
    session = reporting(
        Steps(True, False, True),
        claims_done,
        reports(("Mutants survive in the parser", "normal")),
        claims_done,  # The task the merge pass files.
        reviewers=(files("Kill the parser's mutants", ["F1"], []),),
        triggers=mutation_triggers(**{"mutation-report": MUTATION_REPORT, "merge": SET_MERGE}),
    )
    events = await run_until(session, "completed", "completed", "completed", "completed")

    tasks = board(events)
    assert shown(tasks) == [
        (CODER, "manual", None, 0, "completed"),
        (TESTS_REVIEWER, "mutation-report", None, 0, "completed"),
        (REVIEWER, "merge", None, 0, "completed"),
        (CODER, "merge", "batch-1", 1, "completed"),
    ]
    assert tasks[1].task.prompt == (
        "Look into mutation, failed at base.\n\nMutation tests failed. Its output:\n\n1 test failed"
    )
    assert tasks[1].queued_as == "review"
    assert tasks[2].task.prompt == (
        "Merge the findings of the mutation report at base\n\n"
        f"F1 · {TESTS_REVIEWER} · normal: Mutants survive in the parser\nWhy.\n\nAt most 5."
    )
    assert notices(events) == []


async def test_a_failing_reporting_set_no_trigger_listens_for_gives_its_notice() -> None:
    passed_only = MUTATION_REPORT.model_copy(update={"when": {"status": "passed"}})
    session = reporting(
        Steps(True, False),
        claims_done,
        triggers=mutation_triggers(**{"mutation-report": passed_only}),
    )
    events = await run_until(session, "completed")

    assert notices(events) == [
        "mutation reported: Mutation tests failed. Its output ended:\n\n1 test failed"
    ]


async def test_a_trigger_naming_a_set_there_is_none_of_never_listens() -> None:
    renamed = MUTATION_REPORT.model_copy(update={"when": {"set": "mutants"}})
    session = reporting(
        Steps(True, False), claims_done, triggers=mutation_triggers(**{"report": renamed})
    )
    events = await run_until(session, "completed")

    assert [t.task.role for t in board(events)] == [CODER]
    assert len(notices(events)) == 1


MUTATION_NOTICE = "mutation reported: Mutation tests failed. Its output ended:\n\n1 test failed"
"""The notice of mutation failing, as :class:`~tests.agent_fakes.Steps` fails it."""


def gated(gate: asyncio.Event) -> Script:
    """A task that claims done once ``gate`` opens."""

    async def look(call: Call) -> TurnEndReason:
        await call("get_task")
        await gate.wait()
        await call("complete_task", summary="Looked")
        return "completed"

    return look


async def test_a_listening_trigger_held_back_by_not_queued_leaves_the_notice() -> None:
    gate = asyncio.Event()
    look = Trigger(on="task_landed", role=TESTS_REVIEWER, prompt="Look.", when={"origin": "manual"})
    held = MUTATION_REPORT.model_copy(update={"guards": ("not-queued",)})
    session = reporting(
        Steps(True, False),
        claims_done,
        gated(gate),
        triggers=mutation_triggers(look=look, **{"mutation-report": held}),
    )
    await start(session)
    events: list[Event] = []
    collecting = asyncio.ensure_future(settle(session, events))
    await eventually(lambda: notices(events))
    gate.set()
    await asyncio.wait_for(collecting, 10)
    await session.close()

    assert shown(board(events)) == [
        (CODER, "manual", None, 0, "completed"),
        (TESTS_REVIEWER, "look", None, 0, "completed"),
    ]
    assert notices(events) == [MUTATION_NOTICE]


SMOKE = VerificationStep("Smoke tests", ("smoke",), "smoke")


async def test_a_listening_trigger_that_only_runs_another_set_leaves_the_notice() -> None:
    run_smoke = Trigger.model_validate(
        {"on": "set_finished", "checks": "smoke", "when": {"set": "mutation", "status": "failed"}}
    )
    steps = Steps(True, False, True)
    session = reporting(
        steps,
        claims_done,
        triggers=mutation_triggers(**{"run-smoke": run_smoke}),
        verification=Verification((LINT,), steps, sets=(MUTATION_SET, CheckSet("smoke", (SMOKE,)))),
    )
    exited = agent_exited("set-smoke")
    await start(session)
    events = await asyncio.wait_for(
        settle(session, until=lambda events: any(exited(event) for event in events)), 10
    )
    await session.close()

    assert steps.runs == ["Lint", "Mutation tests", "Smoke tests"]
    assert [t.task.role for t in board(events)] == [CODER]
    assert notices(events) == [MUTATION_NOTICE]


async def test_a_review_rounds_trigger_queues_its_task_with_no_notice_while_reviews_run() -> None:
    rounds = MUTATION_REPORT.model_copy(update={"guards": ("review-rounds",)})
    session = reporting(
        Steps(True, False),
        claims_done,
        claims_done,
        triggers=mutation_triggers(**{"mutation-report": rounds}),
    )
    events = await run_until(session, "completed", "completed")

    assert [t.task.role for t in board(events)] == [CODER, TESTS_REVIEWER]
    assert notices(events) == []


async def test_without_reviews_a_listening_trigger_queues_its_task_with_no_notice() -> None:
    session = reporting(
        Steps(True, False),
        claims_done,
        claims_done,
        triggers=mutation_triggers(**{"mutation-report": MUTATION_REPORT}),
        review=False,
    )
    events = await run_until(session, "completed", "completed")

    assert shown(board(events))[1] == (TESTS_REVIEWER, "mutation-report", None, 0, "completed")
    assert notices(events) == []


PASSED = Trigger(
    on="set_finished",
    role=CODER,
    prompt="{set} {status} at {commit}.\n\n{output}\n\nFrom {trigger}.",
    when={"status": "passed"},
)
"""A project's trigger queuing a coder's task as a set passes."""


async def test_a_passing_run_raises_passed_with_no_output_queuing_an_ordinary_task() -> None:
    session = reporting(Steps(True, True), claims_done, triggers=mutation_triggers(passed=PASSED))
    events = await run_until(session, "completed", "pending")

    tasks = board(events)
    assert shown(tasks) == [
        (CODER, "manual", None, 0, "completed"),
        (CODER, "passed", None, 0, "pending"),
    ]
    assert tasks[1].task.prompt == "mutation passed at base.\n\nFrom passed."
    assert tasks[1].queued_as == "task"
    assert notices(events) == []


async def test_without_reviews_a_trigger_on_a_sets_end_still_queues_its_task() -> None:
    session = reporting(
        Steps(True, True), claims_done, triggers=mutation_triggers(passed=PASSED), review=False
    )
    events = await run_until(session, "completed", "pending")

    assert shown(board(events))[1] == (CODER, "passed", None, 0, "pending")


async def test_the_startup_check_failing_raises_no_set_finished() -> None:
    any_end = PASSED.model_copy(update={"when": {}})
    triggers = mutation_triggers(ended=any_end)
    fix_off = triggers["startup-fix"].model_copy(update={"enabled": False})
    session, _, _ = session_for(
        verification=with_mutation(Steps(False)),
        tree=FakeTree(),
        review=True,
        check_at_startup=True,
        triggers={**triggers, "startup-fix": fix_off},
    )
    events = await events_until(session, agent_exited("startup-check"))
    await settled()
    await session.close()

    assert StartupChecked(passed=False, step="Lint", output="1 test failed") in events
    assert not [e for e in events if isinstance(e, TasksChanged)]
