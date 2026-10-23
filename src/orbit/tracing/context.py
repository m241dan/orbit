"""The current span, kept in a context variable so it follows the code across awaits and into
the tasks started under it."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from orbit.tracing.port import Attributes, AttributeValue, Span, SpanHandle


@dataclass(frozen=True, eq=False)
class NoSpan:
    """A span called ``name`` that records nothing, though it's still current while used."""

    name: str

    def set(self, **attributes: AttributeValue) -> None:
        """Ignore ``attributes``."""

    def set_attributes(self, attributes: Attributes) -> None:
        """Ignore ``attributes``."""

    def event(self, name: str, attributes: Attributes | None = None) -> None:
        """Ignore the event."""

    def error(self, message: str, exception: BaseException | None = None) -> None:
        """Ignore the failure."""

    def end(self) -> None:
        """Do nothing: there's nothing to end."""

    def use(self) -> AbstractContextManager[Span]:
        """Make this span the current one for a block."""
        return use_span(self)


_NONE = NoSpan("none")
_current: ContextVar[Span] = ContextVar("orbit_current_span", default=_NONE)


def current_span() -> Span:
    """The span current here, or a no-op span when there's none."""
    return _current.get()


@contextmanager
def use_span[S: Span](span: S) -> Iterator[S]:
    """Make ``span`` the current one for a block, restoring the one before after it."""
    token = _current.set(span)
    try:
        yield span
    finally:
        _current.reset(token)


@contextmanager
def no_span() -> Iterator[None]:
    """Make no span current for a block, as outside any, restoring the one before after it:
    for starting work that outlives the block, such as a background task, which must not
    record on its starter's span."""
    with use_span(_NONE):
        yield


@contextmanager
def span_block[H: SpanHandle](handle: H) -> Iterator[H]:
    """Make ``handle`` the current span for a block and end it on leaving, recording an error
    escaping the block on it and re-raising it."""
    try:
        with use_span(handle):
            yield handle
    # Cancellation and exits end the span but aren't failures of what it times.
    except Exception as error:
        handle.error(str(error), error)
        raise
    finally:
        handle.end()
