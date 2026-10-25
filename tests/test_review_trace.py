"""Each review's ``orbit.review`` span: starting its own trace, linked to what queued it, with what
it reviewed, the suggestions it made and had refused, and what it cost."""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from orbit.agent import ControlPlane
from orbit.agent.application.review_trace import REVIEW_SPAN, ReviewTracing
from orbit.agent.application.task_trace import TASK_SPAN
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.reviews import ReviewLimits
from orbit.protocol import (
    REVIEWER,
    SUPER_REVIEW,
    SUPER_REVIEWER,
    AddTask,
    FireTrigger,
    RunTasks,
    TaskSpec,
    Usage,
)
from tests.agent_fakes import (
    WORKSPACE,
    FakeTree,
    Script,
    ScriptedLauncher,
    claims_done,
    close,
    send,
    session_for,
    settle,
    suggests,
)
from tests.telemetry_helpers import Traced, linked_to, trace_id

REVIEW_COST = Usage(
    input_tokens=10,
    output_tokens=2,
    cache_creation_input_tokens=3,
    cache_read_input_tokens=4,
    cost_usd=0.5,
)


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


def traced_plane(
    traced: Traced, *scripts: Script, reviewers: Sequence[Script], **options: Any
) -> tuple[ControlPlane, ScriptedLauncher]:
    """A control plane serving a host that reviews, in a tree, whose tasks run through agents
    playing ``scripts`` and whose reviews through agents playing ``reviewers``, both traced by
    ``traced``, and the launcher playing them; ``options`` go to :func:`session_for`."""
    host, _, launcher = session_for(
        *scripts,
        reviewers=reviewers,
        telemetry=traced.telemetry,
        tree=FakeTree(),
        review=True,
        **options,
    )
    return ControlPlane([host], telemetry=traced.telemetry), launcher


async def reviewed(plane: ControlPlane, *tasks: TaskSpec) -> None:
    """Queue ``tasks`` on ``plane`` and run them, waiting until they, their reviews and the
    reviews' suggestions have all finished."""
    for task in tasks:
        await send(plane, AddTask(task=task))
    await send(plane, RunTasks())
    await asyncio.wait_for(settle(plane), 10)


FLAG = TaskSpec(prompt="Add a flag.")


def reviews(traced: Traced) -> list[ReadableSpan]:
    """The reviews' spans, in the order the reviews started."""
    return sorted(traced.spans(REVIEW_SPAN), key=lambda span: span.start_time or 0)


def task_span(traced: Traced, task_id: str) -> ReadableSpan:
    """Task ``task_id``'s one span."""
    tasks = traced.spans(TASK_SPAN)
    [span] = [span for span in tasks if attributes(span)["orbit.task.id"] == task_id]
    return span


def unit_tracing(traced: Traced) -> tuple[ReviewTracing, TaskBoard, str]:
    """Tracing by ``traced``, and a board with one review on it, and its id."""
    board = TaskBoard()
    task_id = board.add_review(TaskSpec(prompt="Review commit c1", role=REVIEWER))
    return ReviewTracing(traced.telemetry, "workspace", lambda: "session"), board, task_id


async def test_a_review_after_landing_links_to_that_task_with_what_it_reviewed() -> None:
    traced = Traced()
    plane, launcher = traced_plane(
        traced,
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests", "Add tests"), suggests()),
    )
    launcher.usages = [(Usage(),), (REVIEW_COST,)]
    await reviewed(plane, FLAG)
    await close(plane)

    review = reviews(traced)[0]
    assert review.parent is None
    assert linked_to(review, task_span(traced, "1"))
    assert dict(attributes(review)) == {
        "orbit.workspace.id": WORKSPACE,
        "orbit.session.id": "session-1",
        "orbit.role": REVIEWER,
        "orbit.review.commits": 1,
        "orbit.review.round": 0,
        "orbit.review.batch": "batch-1",
        "orbit.review.cap": 5,
        "orbit.review.suggestions": 1,
        "orbit.review.refused": 1,
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 2,
        "gen_ai.usage.cache_creation.input_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 4,
        "orbit.cost.usd": 0.5,
    }
    assert review.status.status_code is StatusCode.UNSET


async def test_a_batch_review_links_to_the_task_whose_landing_finished_the_batch() -> None:
    traced = Traced()
    plane, _ = traced_plane(
        traced, claims_done, claims_done, reviewers=(suggests("Add tests"), suggests())
    )
    await reviewed(plane, FLAG)
    await close(plane)

    batch_review = reviews(traced)[1]
    assert linked_to(batch_review, task_span(traced, "3"))
    assert attributes(batch_review)["orbit.review.round"] == 1
    assert attributes(batch_review)["orbit.review.batch"] == "batch-2"


async def test_a_super_review_asked_for_links_to_its_fire_trigger_command() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, reviewers=(suggests(),))
    await send(plane, FireTrigger(trigger=SUPER_REVIEW))
    await asyncio.wait_for(settle(plane), 10)
    await close(plane)

    [review] = traced.spans(REVIEW_SPAN)
    fire_trigger = traced.command("fire_trigger")
    assert linked_to(review, fire_trigger)
    assert trace_id(review) != trace_id(fire_trigger)
    assert attributes(review)["orbit.role"] == SUPER_REVIEWER
    assert attributes(review)["orbit.review.cap"] == 10


async def test_a_super_review_due_on_count_links_to_the_landing_that_reached_it() -> None:
    traced = Traced()
    plane, _ = traced_plane(
        traced,
        claims_done,
        reviewers=(suggests(), suggests()),
        review_limits=ReviewLimits(super_every=1),
    )
    await reviewed(plane, FLAG)
    await close(plane)

    spans = traced.spans(REVIEW_SPAN)
    [whole] = [span for span in spans if attributes(span)["orbit.role"] == SUPER_REVIEWER]
    assert linked_to(whole, task_span(traced, "1"))


async def test_a_review_the_user_queued_records_no_job_and_links_to_nothing() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, reviewers=(suggests("Add tests"),))
    await reviewed(plane, TaskSpec(prompt="Review the parser", role=REVIEWER))
    await close(plane)

    [review] = traced.spans(REVIEW_SPAN)
    assert review.links == ()
    assert not [name for name in attributes(review) if name.endswith(("commits", "round", "cap"))]
    assert "orbit.review.batch" not in attributes(review)
    assert attributes(review)["orbit.review.refused"] == 1


def test_a_review_that_ends_in_error_has_the_error_status() -> None:
    traced = Traced()
    tracing, board, task_id = unit_tracing(traced)

    with tracing.review(board, task_id, None):
        board.finish(task_id, "error", "The reviewer failed.")

    [review] = traced.spans(REVIEW_SPAN)
    assert review.status.status_code is StatusCode.ERROR
    assert review.status.description == "The reviewer failed."
    assert [event.name for event in review.events] == ["exception"]


def test_a_review_broken_by_a_bug_records_the_bug_once() -> None:
    traced = Traced()
    tracing, board, task_id = unit_tracing(traced)

    with pytest.raises(RuntimeError), tracing.review(board, task_id, None):
        board.finish(task_id, "error", "Orbit hit a bug.")
        raise RuntimeError("broken")

    [review] = traced.spans(REVIEW_SPAN)
    assert review.status.status_code is StatusCode.ERROR
    [exception] = review.events
    assert (exception.attributes or {})["exception.message"] == "broken"


def test_a_review_removed_as_it_ends_records_its_suggestions_but_no_usage() -> None:
    traced = Traced()
    tracing, board, task_id = unit_tracing(traced)

    with tracing.review(board, task_id, None) as tally:
        tally.count(refused=False)
        board.remove(task_id)

    [review] = traced.spans(REVIEW_SPAN)
    assert attributes(review)["orbit.review.suggestions"] == 1
    assert "orbit.cost.usd" not in attributes(review)
