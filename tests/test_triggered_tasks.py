"""Triggers firing in a session: the review trigger queues today's reviews as work lands, a
project's triggers queue their own tasks as tasks land or finish, never on their own tasks'
ends, each triggered task's agent starts in its trigger's tab, and each triggered task's span
names its trigger."""

import asyncio
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent.application.lanes import OPEN_GATE
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.review_trace import REVIEW_SPAN
from orbit.agent.application.task_trace import TASK_SPAN
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewLimits, ReviewPlanner, commit_review, review_prompt
from orbit.agent.domain.triggers import Trigger
from orbit.protocol import CODER, REVIEWER, AgentStarted, Event, TaskSpec, TaskState
from tests.agent_fakes import (
    TRIGGERS,
    FakeTree,
    board,
    claims_done,
    desk_for,
    session_for,
    settle,
    start,
)
from tests.lane_fakes import no_work
from tests.telemetry_helpers import Traced

LOOK = Trigger(on="task_landed", role=REVIEWER, prompt="Look at {commit}", when={"role": CODER})
"""A project's trigger queuing a reviewer's look at each landed coder commit."""

TIDY = Trigger(on="task_finished", role=CODER, prompt="Tidy up")
"""A project's trigger queuing a coder's tidying up after each task that finishes."""


def shown(tasks: tuple[TaskState, ...]) -> list[tuple[str, str, str]]:
    """Each of ``tasks``: its role, origin and title."""
    return [(t.task.role, t.origin, t.task.prompt.split("\n", 1)[0]) for t in tasks]


async def ran(*prompts: str, reviewers: int = 0, **options: Any) -> list[TaskState]:
    """The tasks a session, landing work in a tree and reviewing it unless ``options`` say
    otherwise, ends with once ``prompts`` have run and everything they triggered has, with
    ``reviewers`` reviewer agents claiming done; ``options`` go to :func:`session_for`."""
    reviewing: dict[str, Any] = {"tree": FakeTree(), "review": True}
    session, _, _ = session_for(
        *[claims_done] * 4, reviewers=[claims_done] * reviewers, **(reviewing | options)
    )
    await start(session, *prompts)
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    return list(board(events))


async def test_a_coder_task_landing_queues_the_review_it_always_had_from_the_review_trigger() -> (
    None
):
    tasks = await ran("Add a flag.", reviewers=1)
    coder, review = tasks
    assert (review.task.role, review.origin, review.task.skills) == (REVIEWER, "review", ())
    assert review.task.prompt == review_prompt(commit_review(coder.id, "c1"), "diff of c1", 5, ())


async def test_with_the_review_trigger_disabled_landed_work_is_not_reviewed() -> None:
    off = TRIGGERS["review"].model_copy(update={"enabled": False})
    tasks = await ran("Add a flag.", triggers={**TRIGGERS, "review": off})
    assert shown(tuple(tasks)) == [(CODER, "manual", "Add a flag.")]


async def test_a_project_trigger_on_landed_coder_work_queues_a_task_per_landing() -> None:
    tasks = await ran("One.", "Two.", reviewers=4, triggers={**TRIGGERS, "look": LOOK})
    assert sorted(shown(tuple(tasks))) == [
        (CODER, "manual", "One."),
        (CODER, "manual", "Two."),
        (REVIEWER, "look", "Look at c1"),
        (REVIEWER, "look", "Look at c2"),
        (REVIEWER, "review", "Review commit c1"),
        (REVIEWER, "review", "Review commit c2"),
    ]
    assert all(t.status == "completed" for t in tasks)


async def test_a_project_trigger_on_finished_tasks_does_not_fire_on_its_own_tasks_ends() -> None:
    tasks = await ran("Add a flag.", triggers={**TRIGGERS, "tidy": TIDY}, review=False)
    assert shown(tuple(tasks)) == [(CODER, "manual", "Add a flag."), (CODER, "tidy", "Tidy up")]
    assert all(t.status == "completed" for t in tasks)


async def test_a_triggered_tasks_agent_starts_in_its_triggers_tab_and_others_in_none() -> None:
    tidy = TIDY.model_copy(update={"tab": "tidying"})
    session, _, _ = session_for(claims_done, claims_done, triggers={**TRIGGERS, "tidy": tidy})
    await start(session, "Add a flag.")
    events = await asyncio.wait_for(settle(session), 10)
    await session.close()
    started = [(e.task_id, e.tab) for e in events if isinstance(e, AgentStarted)]
    assert started == [("1", None), ("2", "tidying")]


async def test_without_a_review_lane_a_landing_still_fires_the_projects_triggers() -> None:
    look = LOOK.model_copy(update={"role": CODER, "prompt": "Document {commit}"})
    tasks = await ran("Add a flag.", triggers={**TRIGGERS, "document": look}, review=False)
    assert shown(tuple(tasks)) == [
        (CODER, "manual", "Add a flag."),
        (CODER, "document", "Document c1"),
    ]


async def test_triggered_tasks_spans_name_their_triggers() -> None:
    traced = Traced()
    triggers = {**TRIGGERS, "look": LOOK, "tidy": TIDY}
    # The flag's commit is reviewed and looked at, and the tidying's looked at.
    await ran("Add a flag.", reviewers=3, triggers=triggers, telemetry=traced.telemetry)
    tasks = {
        named(span, "orbit.task.title"): named(span, "orbit.trigger")
        for span in traced.spans(TASK_SPAN)
    }
    assert tasks == {"Add a flag.": "manual", "Tidy up": "tidy"}
    reviews = sorted(named(span, "orbit.trigger") for span in traced.spans(REVIEW_SPAN))
    assert reviews == ["look", "look", "review"]


def named(span: ReadableSpan, attribute: str) -> str:
    """The value of ``span``'s ``attribute``, a string."""
    value = (span.attributes or {})[attribute]
    assert isinstance(value, str)
    return value


async def quiet(event: Event) -> None:
    """Publishes nothing."""


async def test_a_review_lane_firing_its_agents_triggers_leaves_other_lanes_tasks_waiting() -> None:
    document = LOOK.model_copy(update={"role": CODER, "prompt": "Document {commit}"})
    desk = desk_for(quiet, tree=FakeTree(), triggers={"document": document})
    tasks = Tasks(TaskBoard(), quiet)
    planner = ReviewPlanner(ReviewLimits())
    lane = ReviewLane(desk, tasks, planner, ReviewHooks(no_work, no_work, OPEN_GATE))
    landed = TaskState(id="1", task=TaskSpec(prompt="Add a flag."), status="completed")
    await lane.landed(landed, "c1")
    assert shown(tasks.board.tasks) == [(CODER, "document", "Document c1")]
    assert not lane.running  # The coder's task waits for the work lane.
    await lane.close()
