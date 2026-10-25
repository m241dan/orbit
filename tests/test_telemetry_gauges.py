"""Live gauges over OpenTelemetry's observable gauges, read as an in-memory reader collects."""

import logging
from collections.abc import Iterable

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge, InMemoryMetricReader, Metric, NumberDataPoint

from orbit.telemetry.gauges import OtelGauges
from orbit.tracing import Attributes, Gauges, Reading

Point = tuple[dict[str, object], float]
"""A gauge's point: its attributes and its value."""


class Collected:
    """Gauges registered with a meter whose metrics an in-memory reader collects."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        meters = MeterProvider(metric_readers=[self.reader])
        self.gauges: Gauges = OtelGauges(meters.get_meter("orbit"))

    def collect(self) -> dict[str, Metric]:
        """Every gauge as collected now, by name; none when no gauge reported a value."""
        data = self.reader.get_metrics_data()
        resources = data.resource_metrics if data is not None else []
        scopes = [scope for resource in resources for scope in resource.scope_metrics]
        return {metric.name: metric for scope in scopes for metric in scope.metrics}


def points(metric: Metric) -> list[Point]:
    """The points of the gauge ``metric``."""
    assert isinstance(metric.data, Gauge)
    return [_point(point) for point in metric.data.data_points]


def _point(point: NumberDataPoint) -> Point:
    return dict(point.attributes or {}), point.value


class Failing:
    """A gauge's read giving ``readings`` in turn, each a list of readings, or raising
    ``RuntimeError`` for ``None``."""

    def __init__(self, *readings: list[Reading] | None) -> None:
        self._readings = list(readings)

    def __call__(self) -> Iterable[Reading]:
        reading = self._readings.pop(0)
        if reading is None:
            raise RuntimeError("dictionary changed size during iteration")
        return reading


@pytest.fixture
def collected() -> Collected:
    return Collected()


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The warnings the gauges logged."""
    return [r.getMessage() for r in caplog.records if r.name == "orbit.telemetry.gauges"]


def test_a_gauge_reports_what_its_read_gives_at_each_collection(collected: Collected) -> None:
    depth: list[float] = [3]
    collected.gauges.register(
        "orbit.queue.depth", "{task}", "Tasks waiting.", lambda: [(depth[0], {})]
    )

    first = collected.collect()["orbit.queue.depth"]
    depth[0] = 1
    second = collected.collect()["orbit.queue.depth"]

    assert (first.unit, first.description) == ("{task}", "Tasks waiting.")
    assert (points(first), points(second)) == ([({}, 3)], [({}, 1)])


def test_a_gauge_reports_a_value_for_each_set_of_attributes_its_read_gives(
    collected: Collected,
) -> None:
    coder: Attributes = {"orbit.role": "coder", "orbit.workspace.id": "a"}
    reviewer: Attributes = {"orbit.role": "reviewer", "orbit.workspace.id": "a"}
    collected.gauges.register(
        "orbit.agents.active", "{agent}", "Agents running.", lambda: [(2, coder), (0, reviewer)]
    )

    assert points(collected.collect()["orbit.agents.active"]) == [(coder, 2), (reviewer, 0)]


def test_a_read_that_raises_is_warned_of_once_and_the_other_gauges_still_report(
    collected: Collected, caplog: pytest.LogCaptureFixture
) -> None:
    collected.gauges.register("orbit.tasks.pending", "{task}", "Pending.", Failing(None, None))
    collected.gauges.register("orbit.queue.depth", "{task}", "Waiting.", lambda: [(2, {})])

    first, second = collected.collect(), collected.collect()

    assert warnings_of(caplog) == [
        "Couldn't read the gauge orbit.tasks.pending: dictionary changed size during iteration"
    ]
    assert "orbit.tasks.pending" not in first and "orbit.tasks.pending" not in second
    assert points(first["orbit.queue.depth"]) == points(second["orbit.queue.depth"]) == [({}, 2)]


def test_a_gauge_read_again_is_warned_of_again_when_its_read_next_raises(
    collected: Collected, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    read = Failing(None, [(1, {})], None)
    collected.gauges.register("orbit.frontends.connected", "{connection}", "Connected.", read)

    read_again = [collected.collect() for _ in range(3)][1]

    assert points(read_again["orbit.frontends.connected"]) == [({}, 1)]
    assert len(warnings_of(caplog)) == 2
