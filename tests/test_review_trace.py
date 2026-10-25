"""Each review's ``orbit.review`` span: starting its own trace, linked to what queued it, with what
it reviewed, the suggestions it made and had refused, and what it cost."""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import Metric
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
    Settings,
    TaskSpec,
    TurnEndReason,
    Usage,
)
from tests.agent_fakes import (
    WORKSPACE,
    Call,
    FakeTree,
    Script,
    ScriptedLauncher,
    claims_done,
    close,
    gives_up,
    host_of,
    lane_of,
    send,
    session_for,
    settle,
    suggests,
)
from tests.telemetry_helpers import Traced, linked_to, trace_id
from tests.waiting import eventually

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


def unit_tracing(
    traced: Traced, settings: Settings | None = None
) -> tuple[ReviewTracing, TaskBoard, str]:
    """Tracing by ``traced``, and a board with one review on it, with ``settings`` if given,
    and its id."""
    board = TaskBoard()
    spec = TaskSpec(prompt="Review commit c1", role=REVIEWER, settings=settings or Settings())
    task_id = board.add_review(spec)
    return ReviewTracing(traced.telemetry, "workspace", lambda: "session"), board, task_id


def recorded(traced: Traced, name: str) -> list[Metric]:
    """The metrics called ``name`` that ``traced`` recorded."""
    data = traced.metrics.get_metrics_data()
    assert data is not None
    scopes = [scope for resource in data.resource_metrics for scope in resource.scope_metrics]
    return [metric for scope in scopes for metric in scope.metrics if metric.name == name]


def points(traced: Traced, name: str) -> list[dict[str, Any]]:
    """The attributes of each point of the metric called ``name`` that ``traced`` recorded."""
    metrics = recorded(traced, name)
    return [dict(point.attributes or {}) for metric in metrics for point in metric.data.data_points]


OPUS = Settings(model="claude-opus-5-5")


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


async def test_a_batch_review_links_to_the_task_whose_ending_without_a_commit_finished_it() -> None:
    traced = Traced()
    plane, _ = traced_plane(
        traced,
        claims_done,
        claims_done,
        gives_up,
        reviewers=(suggests("Add tests", "Rename x"), suggests()),
    )
    await reviewed(plane, FLAG)
    await close(plane)

    batch_review = reviews(traced)[1]
    assert linked_to(batch_review, task_span(traced, "4"))
    assert attributes(batch_review)["orbit.review.round"] == 1


async def test_a_batch_review_links_to_the_review_whose_end_finished_the_batch() -> None:
    traced, suggested = Traced(), asyncio.Event()
    planes: list[ControlPlane] = []

    async def waits_for_suggestion(call: Call) -> TurnEndReason:
        await suggested.wait()
        return await claims_done(call)

    async def suggests_and_waits_for_it_to_land(call: Call) -> TurnEndReason:
        await call("get_task")
        await call("suggest_task", title="Add tests", prompt="Please add tests.")
        suggested.set()
        planner = lane_of(host_of(planes[0])).planner
        await eventually(lambda: "4" in planner.snapshot().batches["batch-1"].finished)
        await call("complete_task", summary="Reviewed")
        return "completed"

    plane, _ = traced_plane(
        traced,
        claims_done,
        waits_for_suggestion,
        claims_done,
        reviewers=(suggests_and_waits_for_it_to_land, suggests(), suggests()),
    )
    planes.append(plane)
    await reviewed(plane, FLAG, TaskSpec(prompt="Rename x."))
    await close(plane)

    first, *rest = reviews(traced)
    [batch_review] = [span for span in rest if attributes(span)["orbit.review.round"] == 1]
    assert attributes(first)["orbit.review.batch"] == "batch-1"
    assert linked_to(batch_review, first)


async def test_a_reviewer_that_gives_up_records_it_on_its_review() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, claims_done, reviewers=(gives_up,))
    await reviewed(plane, FLAG)
    await close(plane)

    [review] = reviews(traced)
    assert [event.name for event in review.events] == ["orbit.task.gave_up"]
    assert not task_span(traced, "1").events


async def test_a_review_that_ran_leaves_the_lane_holding_nothing_of_what_queued_it() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, claims_done, reviewers=(suggests(),))
    await reviewed(plane, FLAG)
    causes = dict(lane_of(host_of(plane))._causes)
    await close(plane)

    [review] = reviews(traced)
    assert linked_to(review, task_span(traced, "1"))
    assert causes == {}


async def test_a_review_stopped_with_the_lane_and_run_again_still_links_to_what_queued_it() -> None:
    traced, started = Traced(), asyncio.Event()

    async def waits(call: Call) -> TurnEndReason:
        await call("get_task")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    plane, _ = traced_plane(traced, claims_done, reviewers=(waits, suggests()))
    await send(plane, AddTask(task=FLAG))
    await send(plane, RunTasks())
    await asyncio.wait_for(started.wait(), 10)
    lane = lane_of(host_of(plane))
    await lane.stop()  # As a halt stops it.
    lane.kick()  # As the halt lifts.
    await asyncio.wait_for(settle(plane), 10)
    await close(plane)

    stopped, again = reviews(traced)
    assert linked_to(stopped, task_span(traced, "1"))
    assert linked_to(again, task_span(traced, "1"))


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


def test_a_review_whose_task_names_a_model_records_it() -> None:
    traced = Traced()
    tracing, board, task_id = unit_tracing(traced, OPUS)

    with tracing.review(board, task_id, None):
        pass

    [review] = traced.spans(REVIEW_SPAN)
    assert attributes(review)["gen_ai.request.model"] == "claude-opus-5-5"


def test_a_review_whose_task_names_no_model_records_none() -> None:
    traced = Traced()
    tracing, board, task_id = unit_tracing(traced)

    with tracing.review(board, task_id, None):
        pass

    [review] = traced.spans(REVIEW_SPAN)
    assert "gen_ai.request.model" not in attributes(review)


def test_a_review_whose_task_names_a_model_records_its_tokens_and_cost_on_it() -> None:
    traced = Traced()
    tracing, board, task_id = unit_tracing(traced, OPUS)

    with tracing.review(board, task_id, None):
        board.add_usage(task_id, REVIEW_COST)

    usage = {"gen_ai.request.model": "claude-opus-5-5", "orbit.role": REVIEWER}
    kinds = ("input", "output", "cache_creation", "cache_read")
    assert points(traced, "gen_ai.client.token.usage") == [
        {**usage, "gen_ai.token.type": kind} for kind in kinds
    ]
    assert points(traced, "orbit.cost") == [usage]
