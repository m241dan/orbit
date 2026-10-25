"""The metrics recorded as spans end, read through an in-memory metric reader."""

from typing import Any

import pytest
from opentelemetry import context, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    DataPointT,
    Histogram,
    HistogramDataPoint,
    InMemoryMetricReader,
    Metric,
    NumberDataPoint,
    Sum,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.types import Attributes, AttributeValue

from orbit.telemetry.metrics import MetricsFromSpans
from orbit.telemetry.otel import otel_telemetry

START = 1_000_000_000_000
"""When each span starts, in nanoseconds."""

Point = tuple[dict[str, Any], float]
"""A metric's point: its attributes and its value, a histogram's the sum of its one record."""


def _value(point: DataPointT) -> float:
    """``point``'s value: a number's own, or the sum of a histogram's one record."""
    if isinstance(point, NumberDataPoint):
        return point.value
    assert isinstance(point, HistogramDataPoint) and point.count == 1
    return point.sum


def _point(point: DataPointT) -> Point:
    """``point``'s attributes and value."""
    return dict(point.attributes or {}), _value(point)


class Recorded:
    """Spans ended through a tracer that records metrics from them, read in memory."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        meters = MeterProvider(metric_readers=[self.reader])
        self.processor = MetricsFromSpans(meters.get_meter("orbit"))
        provider = TracerProvider()
        provider.add_span_processor(self.processor)
        self._tracer = provider.get_tracer("tests")

    def end(self, name: str, attributes: Attributes = None, seconds: float = 1.0) -> None:
        """End a span called ``name`` with ``attributes``, ``seconds`` after it started."""
        span = self._tracer.start_span(name, attributes=attributes, start_time=START)
        span.end(end_time=START + int(seconds * 1e9))

    def metrics(self) -> list[Metric]:
        """Every metric recorded."""
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        scopes = [scope for resource in data.resource_metrics for scope in resource.scope_metrics]
        return [metric for scope in scopes for metric in scope.metrics]

    def metric(self, name: str) -> Metric:
        """The metric called ``name``."""
        [metric] = [metric for metric in self.metrics() if metric.name == name]
        return metric

    def points(self, name: str) -> list[Point]:
        """The points of the metric called ``name``, none if it has none."""
        metrics = [metric for metric in self.metrics() if metric.name == name]
        return [_point(point) for metric in metrics for point in metric.data.data_points]


@pytest.fixture
def recorded() -> Recorded:
    return Recorded()


def test_a_task_span_counts_the_task_its_tokens_and_cost(recorded: Recorded) -> None:
    recorded.end(
        "orbit.task",
        {
            "orbit.task.id": "t1",
            "orbit.task.status": "completed",
            "orbit.role": "coder",
            "orbit.workspace.id": "w",
            "gen_ai.request.model": "m",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 20,
            "gen_ai.usage.cache_creation.input_tokens": 0,
            "gen_ai.usage.cache_read.input_tokens": 0,
            "orbit.cost.usd": 0.5,
        },
    )

    task = {"orbit.task.status": "completed", "orbit.role": "coder", "orbit.workspace.id": "w"}
    assert recorded.points("orbit.tasks") == [(task, 1)]
    usage = {"gen_ai.request.model": "m", "orbit.role": "coder"}
    assert recorded.points("gen_ai.client.token.usage") == [
        ({**usage, "gen_ai.token.type": "input"}, 100),
        ({**usage, "gen_ai.token.type": "output"}, 20),
    ]
    assert recorded.points("orbit.cost") == [(usage, 0.5)]
    assert recorded.points("orbit.credits") == []


def test_a_span_counting_credits_records_them_apart_from_dollars(recorded: Recorded) -> None:
    attributes: dict[str, AttributeValue] = {
        "orbit.role": "coder",
        "gen_ai.request.model": "auto",
        "orbit.cost.credits": 0.62,
    }

    recorded.end("orbit.task", attributes)
    recorded.end("orbit.planner.turn", {"orbit.cost.usd": 0.5, "orbit.cost.credits": 0.25})

    usage = {"gen_ai.request.model": "auto", "orbit.role": "coder"}
    assert recorded.points("orbit.credits") == [(usage, 0.62), ({}, 0.25)]
    assert recorded.points("orbit.cost") == [({}, 0.5)]


def test_each_verify_step_span_records_its_duration_with_its_name_and_result(
    recorded: Recorded,
) -> None:
    passed = {"orbit.verify.step.name": "Unit tests", "orbit.verify.step.result": "passed"}
    failed = {"orbit.verify.step.name": "Ruff lint", "orbit.verify.step.result": "failed"}

    recorded.end("orbit.verify.step", passed, seconds=2.5)
    recorded.end("orbit.verify.step", failed, seconds=0.25)

    assert recorded.points("orbit.verify.step.duration") == [(passed, 2.5), (failed, 0.25)]


def test_an_agent_process_span_records_its_duration_with_its_role_and_runtime(
    recorded: Recorded,
) -> None:
    attributes: dict[str, AttributeValue] = {
        "orbit.agent.id": "agent-3",
        "orbit.role": "coder",
        "orbit.runtime": "claude",
        "orbit.agent.turns": 4,
    }

    recorded.end("orbit.agent.process", attributes, seconds=90)

    assert recorded.points("orbit.agent.duration") == [
        ({"orbit.role": "coder", "orbit.runtime": "claude"}, 90)
    ]


def test_a_review_span_counts_its_suggestions_made_and_refused(recorded: Recorded) -> None:
    recorded.end(
        "orbit.review",
        {"orbit.role": "reviewer", "orbit.review.suggestions": 3, "orbit.review.refused": 1},
    )

    assert recorded.points("orbit.review.suggestions") == [
        ({"orbit.role": "reviewer", "orbit.review.outcome": "made"}, 3),
        ({"orbit.role": "reviewer", "orbit.review.outcome": "refused"}, 1),
    ]


def test_a_review_records_every_kind_of_token_and_its_cost(recorded: Recorded) -> None:
    recorded.end(
        "orbit.review",
        {
            "orbit.role": "super-reviewer",
            "gen_ai.request.model": "m",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 2,
            "gen_ai.usage.cache_creation.input_tokens": 3,
            "gen_ai.usage.cache_read.input_tokens": 4,
            "orbit.cost.usd": 0.25,
        },
    )

    usage = {"gen_ai.request.model": "m", "orbit.role": "super-reviewer"}
    assert recorded.points("gen_ai.client.token.usage") == [
        ({**usage, "gen_ai.token.type": "input"}, 1),
        ({**usage, "gen_ai.token.type": "output"}, 2),
        ({**usage, "gen_ai.token.type": "cache_creation"}, 3),
        ({**usage, "gen_ai.token.type": "cache_read"}, 4),
    ]
    assert recorded.points("orbit.cost") == [(usage, 0.25)]


def test_a_planner_turn_records_its_tokens_and_cost_without_a_role(recorded: Recorded) -> None:
    recorded.end(
        "orbit.planner.turn",
        {
            "gen_ai.request.model": "p",
            "gen_ai.usage.output_tokens": 7,
            "orbit.cost.usd": 0.125,
            "orbit.planner.drafts": 2,
        },
    )

    assert recorded.points("gen_ai.client.token.usage") == [
        ({"gen_ai.request.model": "p", "gen_ai.token.type": "output"}, 7)
    ]
    assert recorded.points("orbit.cost") == [({"gen_ai.request.model": "p"}, 0.125)]


def test_spans_missing_attributes_record_without_them(recorded: Recorded) -> None:
    recorded.end("orbit.task")
    recorded.end("orbit.verify.step", seconds=3)
    recorded.end("orbit.agent.process", seconds=4)
    recorded.end("orbit.review", {"orbit.review.suggestions": 2})

    assert recorded.points("orbit.tasks") == [({}, 1)]
    assert recorded.points("orbit.verify.step.duration") == [({}, 3)]
    assert recorded.points("orbit.agent.duration") == [({}, 4)]
    assert recorded.points("orbit.review.suggestions") == [({"orbit.review.outcome": "made"}, 2)]
    assert recorded.points("gen_ai.client.token.usage") == []
    assert recorded.points("orbit.cost") == []


def test_zero_counts_are_not_recorded(recorded: Recorded) -> None:
    recorded.end(
        "orbit.review",
        {
            "orbit.role": "reviewer",
            "orbit.review.suggestions": 0,
            "orbit.review.refused": 0,
            "gen_ai.usage.input_tokens": 0,
            "gen_ai.usage.output_tokens": 0,
            "orbit.cost.usd": 0.0,
            "orbit.cost.credits": 0.0,
        },
    )

    assert recorded.metrics() == []


def test_counts_that_arent_numbers_are_not_recorded(recorded: Recorded) -> None:
    recorded.end(
        "orbit.planner.turn", {"gen_ai.usage.input_tokens": "many", "orbit.cost.usd": "lots"}
    )

    assert recorded.metrics() == []


@pytest.mark.parametrize(
    "name", ["orbit.command", "orbit.task.run", "orbit.agent.request", "orbit.startup_check"]
)
def test_other_spans_record_nothing(recorded: Recorded, name: str) -> None:
    recorded.end(name, {"orbit.role": "coder", "gen_ai.usage.input_tokens": 5})

    assert recorded.metrics() == []


@pytest.mark.parametrize("name", ["orbit.verify.step", "orbit.agent.process"])
def test_a_span_without_its_times_records_no_duration(recorded: Recorded, name: str) -> None:
    recorded.processor.on_end(ReadableSpan(name=name))

    assert recorded.metrics() == []


def _every_metric(recorded: Recorded) -> None:
    """End spans recording each metric once."""
    usage = {"gen_ai.usage.input_tokens": 10, "orbit.cost.usd": 0.1, "orbit.cost.credits": 0.2}
    recorded.end("orbit.task", usage)
    recorded.end("orbit.verify.step")
    recorded.end("orbit.agent.process")
    recorded.end("orbit.review", {"orbit.review.suggestions": 1})


@pytest.mark.parametrize(
    ("name", "kind", "unit"),
    [
        ("orbit.tasks", Sum, "{task}"),
        ("orbit.verify.step.duration", Histogram, "s"),
        ("orbit.agent.duration", Histogram, "s"),
        ("gen_ai.client.token.usage", Histogram, "{token}"),
        ("orbit.cost", Sum, "USD"),
        ("orbit.credits", Sum, "{credit}"),
        ("orbit.review.suggestions", Sum, "{suggestion}"),
    ],
)
def test_each_metric_has_its_kind_and_unit(
    recorded: Recorded, name: str, kind: type[Sum | Histogram], unit: str
) -> None:
    _every_metric(recorded)

    metric = recorded.metric(name)

    assert isinstance(metric.data, kind)
    assert metric.unit == unit
    assert metric.description
    if isinstance(metric.data, Sum):
        assert metric.data.is_monotonic


def test_token_usage_has_the_genai_conventions_buckets(recorded: Recorded) -> None:
    _every_metric(recorded)

    [point] = recorded.metric("gen_ai.client.token.usage").data.data_points

    assert isinstance(point, HistogramDataPoint)
    assert tuple(point.explicit_bounds) == (
        1,
        4,
        16,
        64,
        256,
        1024,
        4096,
        16384,
        65536,
        262144,
        1048576,
        4194304,
        16777216,
        67108864,
    )


def test_telemetry_records_metrics_from_its_spans_describing_its_resource() -> None:
    reader = InMemoryMetricReader()
    resource = Resource.create({"service.name": "orbit-tests"})
    telemetry = otel_telemetry(InMemorySpanExporter(), reader, resource)
    with telemetry.span("orbit.task", {"orbit.role": "coder"}):
        pass

    data = reader.get_metrics_data()
    telemetry.close()

    assert data is not None
    [resource_metrics] = data.resource_metrics
    assert resource_metrics.resource.attributes["service.name"] == "orbit-tests"
    [scope] = resource_metrics.scope_metrics
    assert scope.scope.name == "orbit"
    assert [metric.name for metric in scope.metrics] == ["orbit.tasks"]


def test_metrics_carry_no_exemplars_even_in_an_opentelemetry_span() -> None:
    reader = InMemoryMetricReader()
    resource = Resource.create({"service.name": "orbit-tests"})
    telemetry = otel_telemetry(InMemorySpanExporter(), reader, resource)
    task = telemetry.start("orbit.task")
    token = context.attach(trace.set_span_in_context(task.otel))
    try:
        task.end()
    finally:
        context.detach(token)

    data = reader.get_metrics_data()
    telemetry.close()

    assert data is not None
    [point] = data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
    assert point.exemplars == []
