"""Orbit's metrics, recorded from the spans they come from as those end."""

from collections.abc import Callable, Mapping, Sequence

from opentelemetry.metrics import Histogram, Meter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.util.types import AttributeValue

Attributes = Mapping[str, AttributeValue]
"""A span's or a measurement's attributes."""

TOKEN_BUCKETS = [4.0**power for power in range(14)]
"""The bucket boundaries OpenTelemetry's GenAI conventions give ``gen_ai.client.token.usage``:
1, 4, 16 and so on to 4 to the 13th."""

TOKENS = (
    ("input", "gen_ai.usage.input_tokens"),
    ("output", "gen_ai.usage.output_tokens"),
    ("cache_creation", "gen_ai.usage.cache_creation.input_tokens"),
    ("cache_read", "gen_ai.usage.cache_read.input_tokens"),
)
"""Each kind of token, as ``gen_ai.token.type`` names it, and the span attribute counting it."""

SUGGESTIONS = (("made", "orbit.review.suggestions"), ("refused", "orbit.review.refused"))
"""Each ``orbit.review.outcome`` of a review's suggestions, and the span attribute counting
them."""


class MetricsFromSpans(SpanProcessor):
    """Records orbit's metrics with ``meter`` as the spans they come from end: tasks, the
    durations of verification steps and agent processes, tokens, cost and review
    suggestions."""

    def __init__(self, meter: Meter) -> None:
        self._tasks = meter.create_counter(
            "orbit.tasks", unit="{task}", description="Task runs through the work lane."
        )
        self._steps = meter.create_histogram(
            "orbit.verify.step.duration", unit="s", description="How long verification steps ran."
        )
        self._agents = meter.create_histogram(
            "orbit.agent.duration", unit="s", description="How long agent processes ran."
        )
        self._tokens = meter.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="Tokens used by tasks, planner turns and reviews.",
            explicit_bucket_boundaries_advisory=TOKEN_BUCKETS,
        )
        self._cost = meter.create_counter(
            "orbit.cost", unit="USD", description="What tasks, planner turns and reviews cost."
        )
        self._suggestions = meter.create_counter(
            "orbit.review.suggestions",
            unit="{suggestion}",
            description="Suggestions reviews made, queued as tasks or refused.",
        )
        self._recorders: dict[str, Callable[[ReadableSpan], None]] = {
            "orbit.task": self._task,
            "orbit.verify.step": self._step,
            "orbit.agent.process": self._agent,
            "orbit.planner.turn": self._turn,
            "orbit.review": self._review,
        }

    def on_end(self, span: ReadableSpan) -> None:
        """Record the metrics ``span`` is a source of, if any."""
        recorder = self._recorders.get(span.name)
        if recorder is not None:
            recorder(span)

    def _task(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        keys = ("orbit.task.status", "orbit.role", "orbit.workspace.id")
        self._tasks.add(1, _picked(attributes, keys))
        self._usage(attributes)

    def _step(self, span: ReadableSpan) -> None:
        keys = ("orbit.verify.step.name", "orbit.verify.step.result")
        _record_duration(self._steps, span, keys)

    def _agent(self, span: ReadableSpan) -> None:
        _record_duration(self._agents, span, ("orbit.role", "orbit.runtime"))

    def _turn(self, span: ReadableSpan) -> None:
        self._usage(span.attributes or {})

    def _review(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        self._usage(attributes)
        role = _picked(attributes, ("orbit.role",))
        for outcome, key in SUGGESTIONS:
            count = _amount(attributes, key)
            if count > 0:
                self._suggestions.add(count, {**role, "orbit.review.outcome": outcome})

    def _usage(self, attributes: Attributes) -> None:
        """Record the tokens and cost ``attributes`` count, each kind of token apart."""
        model = _picked(attributes, ("gen_ai.request.model", "orbit.role"))
        for kind, key in TOKENS:
            count = _amount(attributes, key)
            if count > 0:
                self._tokens.record(count, {**model, "gen_ai.token.type": kind})
        cost = _amount(attributes, "orbit.cost.usd")
        if cost > 0:
            self._cost.add(cost, model)


def _record_duration(histogram: Histogram, span: ReadableSpan, keys: Sequence[str]) -> None:
    """Record how long ``span`` ran, in seconds, in ``histogram``, with its attributes at
    ``keys``; nothing if it lacks a time."""
    if span.start_time is None or span.end_time is None:
        return
    seconds = (span.end_time - span.start_time) / 1e9
    histogram.record(seconds, _picked(span.attributes or {}, keys))


def _picked(attributes: Attributes, keys: Sequence[str]) -> dict[str, AttributeValue]:
    """Those of ``attributes`` at ``keys``, leaving out the ones it lacks."""
    return {key: attributes[key] for key in keys if key in attributes}


def _amount(attributes: Attributes, key: str) -> float:
    """The number ``attributes`` has at ``key``; 0 if it has none."""
    value = attributes.get(key)
    return value if isinstance(value, int | float) else 0
