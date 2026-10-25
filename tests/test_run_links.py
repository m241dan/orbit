"""Each task's ``orbit.task`` span links to what started its run: the ``RunTasks`` or
``FireTrigger`` command, or the review whose suggestions it runs, even once the run starts again
after Claude's usage limit or an outage of its API stopped it."""

import asyncio
from collections.abc import Mapping
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from orbit.agent import ControlPlane
from orbit.agent.application.limit_pause import LimitPause
from orbit.agent.application.review_trace import REVIEW_SPAN
from orbit.agent.application.task_trace import TASK_SPAN
from orbit.protocol import (
    CONFIGURE_CHECKS,
    AddTask,
    FireTrigger,
    Notice,
    ResumeStorage,
    RunTasks,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import (
    START,
    Call,
    Configurator,
    FakeStore,
    FakeTree,
    Script,
    ScriptedLauncher,
    claims_done,
    close,
    events_until,
    idle,
    no_steps,
    payloads,
    publish,
    run,
    send,
    session_for,
    settle,
    suggests,
)
from tests.connection_helpers import quiet
from tests.telemetry_helpers import Traced, linked_to, trace_id

LIMIT = "You've hit your session limit · resets 6pm (UTC)"
OVERLOADED = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}'


class Waits:
    """A pause whose clock stands at :data:`START` and whose sleeps last until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        await self.release.wait()

    @property
    def pause(self) -> LimitPause:
        return LimitPause(now=lambda: START, sleep=self.sleep)


def attributes(span: ReadableSpan) -> Mapping[str, Any]:
    assert span.attributes is not None
    return span.attributes


def traced_plane(
    traced: Traced, *scripts: Script, **options: Any
) -> tuple[ControlPlane, ScriptedLauncher]:
    """A control plane serving a host whose tasks run through agents playing ``scripts``, both
    traced by ``traced``, and the launcher playing them; ``options`` go to :func:`session_for`."""
    host, _, launcher = session_for(*scripts, telemetry=traced.telemetry, **options)
    return ControlPlane([host], telemetry=traced.telemetry), launcher


async def ran(plane: ControlPlane, *prompts: str) -> None:
    """Queue tasks with ``prompts`` on ``plane``, run them, and wait for the run to end."""
    for prompt in prompts:
        await send(plane, AddTask(task=TaskSpec(prompt=prompt)))
    await send(plane, RunTasks())
    await events_until(plane, idle())


def task_spans(traced: Traced, task_id: str) -> list[ReadableSpan]:
    """The spans of task ``task_id``'s runs."""
    return [
        span for span in traced.spans(TASK_SPAN) if attributes(span)["orbit.task.id"] == task_id
    ]


async def errs(call: Call) -> TurnEndReason:
    return "error"


def stopped_by(traced: Traced, waits: Waits, error: str, **options: Any) -> ControlPlane:
    """A traced control plane whose first agent's turn ends in ``error``, then claims each task
    done; ``options`` go to :func:`session_for`."""
    plane, launcher = traced_plane(
        traced, errs, claims_done, claims_done, pause=waits.pause, **options
    )
    launcher.turn_errors.append(error)
    return plane


def limited(traced: Traced, waits: Waits, **options: Any) -> ControlPlane:
    """A traced control plane whose first agent hits the usage limit, then claims each task
    done; ``options`` go to :func:`session_for`."""
    return stopped_by(traced, waits, LIMIT, **options)


async def test_each_task_of_a_run_links_to_its_run_tasks_command_from_its_own_trace() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, claims_done, claims_done)
    await ran(plane, "a", "b")
    await close(plane)

    run_tasks = traced.command("run_tasks")
    tasks = traced.spans(TASK_SPAN)
    assert len(tasks) == 2
    for task in tasks:
        assert task.parent is None
        assert linked_to(task, run_tasks)
        assert trace_id(task) != trace_id(run_tasks)


async def test_tasks_run_after_a_usage_limit_pause_still_link_to_the_run_tasks_command() -> None:
    traced, waits = Traced(), Waits()
    plane = limited(traced, waits)
    await ran(plane, "a", "b")
    waits.release.set()
    await events_until(plane, idle())
    await close(plane)

    run_tasks = traced.command("run_tasks")
    tasks = traced.spans(TASK_SPAN)
    assert [attributes(task)["orbit.task.id"] for task in tasks] == ["1", "1", "2"]
    assert all(linked_to(task, run_tasks) for task in tasks)


async def test_a_run_started_again_once_a_halt_lifts_links_to_the_run_tasks_command() -> None:
    traced, waits, store = Traced(), Waits(), FakeStore()
    plane = limited(traced, waits, store=store)
    await ran(plane, "a")
    store.append_failure = "disk full"
    [host] = plane.hosts
    await publish(host, Notice(message="n"))
    waits.release.set()
    await quiet(payloads(plane))
    store.append_failure = None
    await send(plane, ResumeStorage())
    await events_until(plane, idle())
    await close(plane)

    run_tasks = traced.command("run_tasks")
    again = task_spans(traced, "1")[-1]
    assert attributes(again)["orbit.task.status"] == "completed"
    assert linked_to(again, run_tasks)


async def test_tasks_run_after_an_outage_pause_still_link_to_the_run_tasks_command() -> None:
    traced, waits = Traced(), Waits()
    plane = stopped_by(traced, waits, OVERLOADED)
    await ran(plane, "a", "b")
    waits.release.set()
    await events_until(plane, idle())
    await close(plane)

    run_tasks = traced.command("run_tasks")
    tasks = traced.spans(TASK_SPAN)
    assert [attributes(task)["orbit.task.id"] for task in tasks] == ["1", "1", "2"]
    assert all(linked_to(task, run_tasks) for task in tasks)


async def test_a_run_an_outage_stopped_started_again_once_a_halt_lifts_links_to_run_tasks() -> None:
    traced, waits, store = Traced(), Waits(), FakeStore()
    plane = stopped_by(traced, waits, OVERLOADED, store=store)
    await ran(plane, "a")
    store.append_failure = "disk full"
    [host] = plane.hosts
    await publish(host, Notice(message="n"))
    waits.release.set()
    await quiet(payloads(plane))
    store.append_failure = None
    await send(plane, ResumeStorage())
    await events_until(plane, idle())
    await close(plane)

    run_tasks = traced.command("run_tasks")
    again = task_spans(traced, "1")[-1]
    assert attributes(again)["orbit.task.status"] == "completed"
    assert linked_to(again, run_tasks)


async def test_a_configurator_run_links_to_its_fire_trigger_command() -> None:
    traced = Traced()
    plane, _ = traced_plane(traced, Configurator(), verification=no_steps())
    await send(plane, FireTrigger(trigger=CONFIGURE_CHECKS))
    await events_until(plane, idle())
    await close(plane)

    assert linked_to(traced.task(), traced.command("fire_trigger"))


async def test_a_run_of_the_work_a_review_queued_links_its_tasks_to_that_review() -> None:
    traced = Traced()
    plane, _ = traced_plane(
        traced,
        claims_done,
        claims_done,
        reviewers=(suggests("Add tests"), suggests()),
        tree=FakeTree(),
        review=True,
    )
    await send(plane, AddTask(task=TaskSpec(prompt="Add a flag.")))
    await send(plane, RunTasks())
    await asyncio.wait_for(settle(plane), 10)
    await close(plane)

    first_review = min(traced.spans(REVIEW_SPAN), key=lambda span: span.start_time or 0)
    [suggestion] = task_spans(traced, "3")
    assert attributes(suggestion)["orbit.trigger"] == "review"
    assert linked_to(suggestion, first_review)


async def test_a_run_started_outside_any_command_span_links_to_nothing() -> None:
    traced = Traced()
    session, _, _ = session_for(claims_done, telemetry=traced.telemetry)
    await run(session)  # Through a control plane that traces nothing.

    assert traced.task().links == ()
