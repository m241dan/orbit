"""The tracing port: the current span, and the no-op telemetry that still nests spans."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

import pytest

from orbit.protocol import EventEnvelope, Notice
from orbit.tracing import (
    Attributes,
    AttributeValue,
    NoSpan,
    NoTelemetry,
    Span,
    Telemetry,
    current_span,
    no_span,
    span_block,
    use_span,
)

TELEMETRY: Telemetry = NoTelemetry()


def test_outside_any_block_the_current_span_is_a_no_op_span() -> None:
    assert isinstance(current_span(), NoSpan)


def test_a_span_block_makes_its_span_current_inside_it() -> None:
    with TELEMETRY.span("orbit.task") as span:
        assert current_span() is span


def test_a_no_span_block_makes_none_current_and_restores_the_one_before() -> None:
    outside = current_span()
    with TELEMETRY.span("orbit.task") as span:
        with no_span():
            assert current_span() is outside
        assert current_span() is span


def test_a_nested_span_block_restores_the_outer_span_after_it() -> None:
    outside = current_span()
    with TELEMETRY.span("orbit.task") as outer:
        with TELEMETRY.span("orbit.task.run") as inner:
            assert current_span() is inner
            assert inner is not outer
        assert current_span() is outer
    assert current_span() is outside


def test_an_exception_escaping_a_span_block_is_re_raised_and_the_span_restored() -> None:
    outside = current_span()
    with pytest.raises(ValueError, match="boom"), TELEMETRY.span("orbit.task"):
        raise ValueError("boom")
    assert current_span() is outside


async def test_a_task_created_in_a_span_block_sees_its_span() -> None:
    async def seen() -> Span:
        return current_span()

    with TELEMETRY.span("orbit.task") as span:
        task = asyncio.create_task(seen())
    assert await task is span


def test_a_started_span_is_current_only_while_used() -> None:
    handle = TELEMETRY.start("orbit.task", {"orbit.task.id": "t1"})
    outside = current_span()
    with handle.use() as span:
        assert span is handle
        assert current_span() is handle
    assert current_span() is outside
    handle.end()


def test_a_no_op_span_ignores_its_links_but_still_nests() -> None:
    linked = TELEMETRY.start("orbit.command")
    handle = TELEMETRY.start("orbit.task", links=[linked])
    with TELEMETRY.span("orbit.review", links=[linked, handle]) as span:
        assert current_span() is span
    assert isinstance(handle, NoSpan) and handle.name == "orbit.task"


EVENT = EventEnvelope(seq=1, payload=Notice(message="hi"))
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_no_telemetry_carries_an_envelope_unchanged() -> None:
    with TELEMETRY.span("orbit.command"):
        assert TELEMETRY.carry(EVENT) is EVENT


def test_no_telemetry_finds_a_no_op_span_in_an_envelope_carrying_a_trace_context() -> None:
    carried = TELEMETRY.carried(EVENT.model_copy(update={"traceparent": TRACEPARENT}))
    assert isinstance(carried, NoSpan)


def test_a_no_op_span_ignores_what_it_records() -> None:
    span = NoSpan("orbit.task")
    attributes: dict[str, AttributeValue] = {"orbit.role": "coder", "orbit.task.fix_attempts": 2}
    span.set(**attributes)
    span.set_attributes({"orbit.cost.usd": 0.5, "orbit.checks": ["lint", "tests"]})
    span.event("orbit.verify.step", {"passed": True})
    span.event("orbit.verify.step")
    span.error("failed", ValueError("boom"))
    span.error("failed")
    span.end()
    assert span.name == "orbit.task"


@dataclass
class RecordedSpan:
    """A span that remembers its failures and whether it ended."""

    errors: list[tuple[str, BaseException | None]] = field(default_factory=list)
    ended: bool = False

    def set(self, **attributes: AttributeValue) -> None:
        pass

    def set_attributes(self, attributes: Attributes) -> None:
        pass

    def event(self, name: str, attributes: Attributes | None = None) -> None:
        pass

    def error(self, message: str, exception: BaseException | None = None) -> None:
        self.errors.append((message, exception))

    def end(self) -> None:
        self.ended = True

    def use(self) -> AbstractContextManager[Span]:
        return use_span(self)


def test_a_span_block_ends_its_span() -> None:
    span = RecordedSpan()
    with span_block(span):
        assert current_span() is span
    assert span.ended
    assert span.errors == []


def test_an_exception_escaping_a_span_block_is_recorded_as_its_error() -> None:
    span = RecordedSpan()
    boom = ValueError("boom")
    with pytest.raises(ValueError), span_block(span):
        raise boom
    assert span.errors == [("boom", boom)]
    assert span.ended


async def test_a_cancelled_span_block_ends_its_span_without_an_error() -> None:
    span = RecordedSpan()

    async def waits() -> None:
        with span_block(span):
            await asyncio.Event().wait()

    task = asyncio.create_task(waits())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert span.ended
    assert span.errors == []
