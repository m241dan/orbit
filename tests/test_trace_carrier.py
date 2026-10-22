"""The trace context an envelope carries, as a carrier of W3C trace headers."""

from orbit.protocol import (
    CommandEnvelope,
    EventEnvelope,
    Notice,
    RemoveTask,
    trace_carrier,
    with_trace_carrier,
)

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACESTATE = "orbit=1"


def command(traceparent: str | None = None, tracestate: str | None = None) -> CommandEnvelope:
    """An envelope for a command with the trace context given."""
    return CommandEnvelope(
        command_id="c1",
        payload=RemoveTask(task_id="t1"),
        traceparent=traceparent,
        tracestate=tracestate,
    )


def test_an_envelope_without_trace_context_has_an_empty_carrier() -> None:
    assert trace_carrier(command()) == {}


def test_the_carrier_holds_only_the_fields_set() -> None:
    assert trace_carrier(command(TRACEPARENT)) == {"traceparent": TRACEPARENT}


def test_the_carrier_holds_both_fields_when_both_are_set() -> None:
    carrier = trace_carrier(command(TRACEPARENT, TRACESTATE))
    assert carrier == {"traceparent": TRACEPARENT, "tracestate": TRACESTATE}


def test_with_trace_carrier_sets_both_fields_and_ignores_other_keys() -> None:
    carrier = {"traceparent": TRACEPARENT, "tracestate": TRACESTATE, "baggage": "k=v"}
    envelope = with_trace_carrier(command(), carrier)
    assert envelope == command(TRACEPARENT, TRACESTATE)


def test_with_trace_carrier_leaves_the_original_envelope_unchanged() -> None:
    original = command()
    with_trace_carrier(original, {"traceparent": TRACEPARENT})
    assert original.traceparent is None


def test_with_trace_carrier_keeps_an_event_envelope_an_event_envelope() -> None:
    event = EventEnvelope(seq=1, payload=Notice(message="hi"))
    traced = with_trace_carrier(event, {"traceparent": TRACEPARENT})
    assert traced == EventEnvelope(seq=1, payload=Notice(message="hi"), traceparent=TRACEPARENT)
