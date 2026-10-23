"""Holding back orbit's and OpenTelemetry's warnings while the TUI draws on the terminal."""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

CAPTURED = ("orbit", "opentelemetry")
"""The loggers, with those under them, whose warnings are captured."""

ShowWarning = Callable[[str, str], object]
"""Shows a warning, given the logger's name and the message."""


class WarningCapture(logging.Handler):
    """Captures the WARNING-and-above records of orbit's and OpenTelemetry's loggers while
    :meth:`capturing`, keeping every one for :meth:`lines` and showing each kind, its logger and
    message template, once per run through what :meth:`attach` is given.

    A kind once shown isn't shown again that run (the telemetry plan's decision 12): libraries
    rarely log recovery. Records may come from any thread; they're shown on the loop that
    attached.
    """

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []  # Every record captured, in order.
        self._kinds: set[tuple[str, str]] = set()
        self._unshown: list[tuple[str, str]] = []  # Kinds to show once attached.
        self._attached: tuple[asyncio.AbstractEventLoop, ShowWarning] | None = None
        self._guard = threading.RLock()  # Held over all of the above.

    @contextmanager
    def capturing(self) -> Iterator[None]:
        """Run the block with the loggers' records coming here, and not to the terminal."""
        for name in CAPTURED:
            logging.getLogger(name).addHandler(self)
        try:
            yield
        finally:
            for name in CAPTURED:
                logging.getLogger(name).removeHandler(self)

    def attach(self, show: ShowWarning) -> None:
        """Show each new kind of warning with ``show``, on the running loop, starting with
        those captured before."""
        with self._guard:
            self._attached = (asyncio.get_running_loop(), show)
            unshown, self._unshown = self._unshown, []
        for warning in unshown:
            show(*warning)

    def detach(self) -> None:
        """Stop showing warnings; they're still captured."""
        with self._guard:
            self._attached = None

    def emit(self, record: logging.LogRecord) -> None:
        """Keep ``record``, and show it if it's the first of its kind."""
        with self._guard:
            self._keep(record)

    def _keep(self, record: logging.LogRecord) -> None:
        """Keep ``record``, and show it if it's the first of its kind."""
        self.records.append(record)
        kind = (record.name, str(record.msg))
        if kind in self._kinds:
            return
        self._kinds.add(kind)
        warning = (record.name, record.getMessage())
        if self._attached is None:
            self._unshown.append(warning)
            return
        loop, show = self._attached
        with suppress(RuntimeError):  # The loop has closed: nobody is showing them.
            loop.call_soon_threadsafe(show, *warning)

    def lines(self) -> list[str]:
        """Every record captured, formatted, in order."""
        with self._guard:
            return [self.format(record) for record in self.records]
