"""Profiling for finding what blocks orbit's event loops."""

import asyncio
import cProfile
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO


@dataclass
class LoopWatchdog:
    """Logs each time ``loop``, running in thread ``thread_id``, is blocked for over
    ``threshold`` seconds, with what that thread was running, checking every ``interval``."""

    loop: asyncio.AbstractEventLoop
    thread_id: int
    name: str
    log: TextIO
    threshold: float = 0.05
    interval: float = 0.01
    _thread: threading.Thread | None = field(default=None, init=False)
    _stopping: threading.Event = field(default_factory=threading.Event, init=False)
    _pending: threading.Event = field(default_factory=threading.Event, init=False)

    def start(self) -> None:
        """Start watching the loop from a daemon thread."""
        self._thread = threading.Thread(target=self._watch, name=f"{self.name}-watchdog")
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and wait for the watching thread to end."""
        if self._thread is None:
            return
        self._stopping.set()
        # The loop's own thread usually calls this, so it can't run a pending check.
        self._pending.set()
        self._thread.join()

    def _watch(self) -> None:
        """Check the loop until stopped."""
        while not self._stopping.is_set():
            self._check()
            self._stopping.wait(self.interval)

    def _check(self) -> None:
        """Schedule a no-op on the loop and log a block if it doesn't run in time."""
        ran = self._pending = threading.Event()
        started = time.monotonic()
        self.loop.call_soon_threadsafe(ran.set)
        if ran.wait(self.threshold) or self._stopping.is_set():
            return
        self._write(
            f"{_now()} {self.name} blocked for {_ms(time.monotonic() - started)} so far;"
            f" its thread was running:\n{self._stack()}"
        )
        ran.wait()
        self._write(f"{_now()} {self.name} unblocked after {_ms(time.monotonic() - started)}\n")

    def _stack(self) -> str:
        """The stack of the loop's thread, formatted."""
        return "".join(traceback.format_stack(sys._current_frames()[self.thread_id]))

    def _write(self, record: str) -> None:
        """Write ``record`` to the log, flushed so it survives a crash."""
        self.log.write(record)
        self.log.flush()


def _now() -> str:
    """The local time now, to the millisecond."""
    return datetime.now().isoformat(sep=" ", timespec="milliseconds")


def _ms(seconds: float) -> str:
    """``seconds`` in whole milliseconds."""
    return f"{seconds * 1000:.0f} ms"


@contextmanager
def profiled(directory: Path, name: str) -> Iterator[None]:
    """Profile the current thread, and log each time its running loop is blocked, while
    the block runs; writes ``name``.prof and ``name``-blocked.log to ``directory``.

    Only one profile can be on at a time, and it sees every thread, so if another thread's
    is already on, this writes only the log."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}-blocked.log").open("w") as log:
        watchdog = LoopWatchdog(asyncio.get_running_loop(), threading.get_ident(), name, log)
        watchdog.start()
        try:
            with _profile(directory / f"{name}.prof"):
                yield
        finally:
            watchdog.stop()


@contextmanager
def _profile(path: Path) -> Iterator[None]:
    """Profile while the block runs and write the profile to ``path``, unless a profile is
    already on."""
    profile = cProfile.Profile()
    try:
        profile.enable()
    except ValueError:  # Another profile is on.
        yield
        return
    try:
        yield
    finally:
        profile.disable()
        profile.dump_stats(path)
