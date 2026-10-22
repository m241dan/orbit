"""Spans made through the port, exported by the OpenTelemetry SDK."""

import asyncio
import logging
import threading
import time
from collections.abc import Sequence

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from orbit.protocol import (
    PROTOCOL_VERSION,
    CommandEnvelope,
    EventEnvelope,
    Hello,
    Notice,
    trace_carrier,
)
from orbit.telemetry.otel import OtelTelemetry, otel_telemetry
from orbit.tracing import current_span

RESOURCE = Resource.create({"service.name": "orbit-tests"})


def _telemetry(exporter: SpanExporter, flush_seconds: float = 5.0) -> OtelTelemetry:
    return otel_telemetry(exporter, RESOURCE, flush_seconds)


def _exported(build: "Build") -> dict[str, ReadableSpan]:
    """The spans ``build`` exported, by name, once its telemetry is closed."""
    build.telemetry.close()
    return {span.name: span for span in build.exporter.get_finished_spans()}


class Build:
    """Telemetry exporting to memory."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.telemetry = _telemetry(self.exporter)


def test_nested_spans_are_parent_and_child_in_one_trace() -> None:
    build = Build()
    with build.telemetry.span("orbit.task"), build.telemetry.span("orbit.task.run"):
        pass

    spans = _exported(build)
    task, run = spans["orbit.task"], spans["orbit.task.run"]
    assert task.parent is None
    assert run.parent is not None and task.context is not None
    assert run.parent.span_id == task.context.span_id
    assert run.context is not None and run.context.trace_id == task.context.trace_id


async def test_a_span_in_a_task_started_in_a_span_is_its_child() -> None:
    build = Build()

    async def run() -> None:
        with build.telemetry.span("orbit.task.run"):
            await asyncio.sleep(0)

    with build.telemetry.span("orbit.task"):
        task = asyncio.create_task(run())
    await task

    spans = _exported(build)
    parent = spans["orbit.task.run"].parent
    task_context = spans["orbit.task"].context
    assert parent is not None and task_context is not None
    assert parent.span_id == task_context.span_id


def test_a_span_outside_any_other_starts_its_own_trace() -> None:
    build = Build()
    with build.telemetry.span("orbit.task"):
        pass
    with build.telemetry.span("orbit.task"):
        pass

    build.telemetry.close()
    first, second = build.exporter.get_finished_spans()
    assert first.parent is None and second.parent is None
    assert first.context is not None and second.context is not None
    assert first.context.trace_id != second.context.trace_id


def test_attributes_and_events_are_recorded_on_the_span() -> None:
    build = Build()
    with build.telemetry.span("orbit.task", {"orbit.task.id": "t1"}) as span:
        span.set(title="Fix it", attempts=2)
        span.set_attributes({"orbit.role": "coder"})
        span.event("orbit.verify.step", {"name": "lint", "result": "passed"})
        span.event("orbit.give_up")

    task = _exported(build)["orbit.task"]
    assert task.attributes == {
        "orbit.task.id": "t1",
        "title": "Fix it",
        "attempts": 2,
        "orbit.role": "coder",
    }
    assert [(event.name, dict(event.attributes or {})) for event in task.events] == [
        ("orbit.verify.step", {"name": "lint", "result": "passed"}),
        ("orbit.give_up", {}),
    ]


def test_an_error_escaping_a_span_marks_it_failed_with_an_exception_event() -> None:
    build = Build()
    with pytest.raises(RuntimeError), build.telemetry.span("orbit.task"):
        raise RuntimeError("merge conflict")

    task = _exported(build)["orbit.task"]
    assert task.status.status_code is StatusCode.ERROR
    assert task.status.description == "merge conflict"
    [event] = task.events
    assert event.name == "exception"
    assert (event.attributes or {})["exception.message"] == "merge conflict"


def test_an_error_without_an_exception_records_no_event() -> None:
    build = Build()
    with build.telemetry.span("orbit.task") as span:
        span.error("gave up")

    task = _exported(build)["orbit.task"]
    assert task.status.status_code is StatusCode.ERROR
    assert task.events == ()


def test_a_started_span_runs_until_ended_and_parents_spans_while_used() -> None:
    build = Build()
    handle = build.telemetry.start("orbit.task")
    with handle.use() as used:
        assert current_span() is used
        with build.telemetry.span("orbit.task.wait"):
            pass
    assert build.exporter.get_finished_spans() == ()
    handle.end()

    spans = _exported(build)
    parent = spans["orbit.task.wait"].parent
    task_context = spans["orbit.task"].context
    assert parent is not None and task_context is not None
    assert parent.span_id == task_context.span_id


HELLO = CommandEnvelope(
    command_id="c1", payload=Hello(client="tests", protocol_version=PROTOCOL_VERSION)
)


def test_an_envelope_carries_the_current_spans_trace_context() -> None:
    build = Build()
    with build.telemetry.span("orbit.command"):
        carried = build.telemetry.carry(HELLO)
    build.telemetry.close()

    [command] = build.exporter.get_finished_spans()
    assert command.context is not None and carried.traceparent is not None
    _, trace_id, span_id, _ = carried.traceparent.split("-")
    assert (int(trace_id, 16), int(span_id, 16)) == (
        command.context.trace_id,
        command.context.span_id,
    )


def test_outside_any_span_an_envelope_carries_no_trace_context() -> None:
    build = Build()

    carried = build.telemetry.carry(HELLO)
    build.telemetry.close()

    assert trace_carrier(carried) == {}


def test_spans_under_a_carried_context_continue_its_trace() -> None:
    sender, receiver = Build(), Build()
    with sender.telemetry.span("orbit.command"):
        carried = sender.telemetry.carry(EventEnvelope(seq=1, payload=Notice(message="hi")))
    with receiver.telemetry.carried(carried).use(), receiver.telemetry.span("orbit.agent.request"):
        pass

    command = _exported(sender)["orbit.command"]
    request = _exported(receiver)["orbit.agent.request"]
    assert request.parent is not None and command.context is not None
    assert request.parent.span_id == command.context.span_id
    assert request.parent.is_remote


def test_an_envelope_without_trace_context_parents_nothing() -> None:
    build = Build()
    with build.telemetry.carried(HELLO).use(), build.telemetry.span("orbit.command"):
        pass

    assert _exported(build)["orbit.command"].parent is None


def test_spans_carry_the_resource() -> None:
    build = Build()
    with build.telemetry.span("orbit.task"):
        pass

    assert _exported(build)["orbit.task"].resource.attributes["service.name"] == "orbit-tests"


class Failing(SpanExporter):
    """An exporter that can't export."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.FAILURE


class Stuck(SpanExporter):
    """An exporter whose exports hang until released."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.released.wait(10)
        return SpanExportResult.SUCCESS


def test_closing_warns_once_naming_the_spans_that_couldnt_be_exported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry = _telemetry(Failing())
    for _ in range(3):
        with telemetry.span("orbit.task"):
            pass

    with caplog.at_level(logging.WARNING):
        telemetry.close()

    assert [record.getMessage() for record in caplog.records] == [
        "Telemetry dropped 3 spans it couldn't export."
    ]


def test_closing_after_every_span_is_exported_warns_of_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    build = Build()
    with build.telemetry.span("orbit.task"):
        pass

    with caplog.at_level(logging.WARNING):
        build.telemetry.close()

    assert caplog.records == []
    assert len(build.exporter.get_finished_spans()) == 1


def test_closing_waits_no_longer_than_the_flush_time(caplog: pytest.LogCaptureFixture) -> None:
    stuck = Stuck()
    telemetry = _telemetry(stuck, flush_seconds=0.2)
    with telemetry.span("orbit.task"):
        pass

    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        telemetry.close()
    took = time.monotonic() - started
    stuck.released.set()

    assert took < 2
    assert [record.getMessage() for record in caplog.records] == [
        "Telemetry dropped 1 spans it couldn't export."
    ]
