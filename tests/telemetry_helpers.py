"""Telemetry exporting its spans to memory, and the trace context naming them."""

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orbit.agent.application.command_trace import COMMAND_SPAN
from orbit.telemetry.otel import OtelTelemetry, otel_telemetry


class Traced:
    """Telemetry exporting its spans to memory."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.telemetry: OtelTelemetry = otel_telemetry(
            self.exporter, Resource.create({"service.name": "orbit-tests"})
        )

        self._closed = False

    def spans(self, name: str) -> list[ReadableSpan]:
        """The spans called ``name`` exported, once every span is."""
        if not self._closed:
            self._closed = True
            self.telemetry.close()
        return [span for span in self.exporter.get_finished_spans() if span.name == name]

    def task(self) -> ReadableSpan:
        """The one task's span."""
        [task] = self.spans("orbit.task")
        return task

    def command(self, kind: str) -> ReadableSpan:
        """The one ``orbit.command`` span of a command of type ``kind``."""
        commands = self.spans(COMMAND_SPAN)
        [span] = [
            span for span in commands if (span.attributes or {})["orbit.command.type"] == kind
        ]
        return span


def linked_to(span: ReadableSpan, target: ReadableSpan) -> bool:
    """Whether ``span`` links to ``target``, and to nothing else."""
    assert target.context is not None
    return [link.context.span_id for link in span.links] == [target.context.span_id]


def trace_id(span: ReadableSpan) -> int:
    """The id of ``span``'s trace."""
    assert span.context is not None
    return int(span.context.trace_id)


def traceparent_of(span: ReadableSpan) -> str:
    """The W3C ``traceparent`` naming ``span``, with its flags, as an envelope carries it."""
    context = span.context
    assert context is not None
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{context.trace_flags:02x}"
