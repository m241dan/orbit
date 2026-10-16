import asyncio
import io
import pstats
import threading
import time
from pathlib import Path

from orbit.profiling import LoopWatchdog, profiled


def hogs_the_loop() -> None:
    time.sleep(0.4)


def watchdog(log: io.StringIO) -> LoopWatchdog:
    """A watchdog on the running loop and this thread, writing to ``log``."""
    return LoopWatchdog(asyncio.get_running_loop(), threading.get_ident(), "ui", log, 0.1)


async def test_a_blocked_loop_logs_one_record_with_what_was_running() -> None:
    log = io.StringIO()
    dog = watchdog(log)
    dog.start()
    await asyncio.sleep(0.05)
    hogs_the_loop()
    await asyncio.sleep(0.05)
    dog.stop()

    text = log.getvalue()
    assert text.count("blocked for") == 1
    assert "ui blocked for" in text
    assert "hogs_the_loop" in text
    assert text.count("unblocked after") == 1


async def test_a_loop_that_keeps_running_logs_nothing() -> None:
    log = io.StringIO()
    dog = watchdog(log)
    dog.start()
    await asyncio.sleep(0.3)
    dog.stop()

    assert log.getvalue() == ""


def test_stopping_a_watchdog_that_never_started_does_nothing() -> None:
    loop = asyncio.new_event_loop()
    try:
        LoopWatchdog(loop, threading.get_ident(), "ui", io.StringIO()).stop()
    finally:
        loop.close()


async def test_profiled_writes_a_profile_and_a_blocked_log(tmp_path: Path) -> None:
    directory = tmp_path / "profile" / "run"
    with profiled(directory, "backend"):
        await asyncio.sleep(0.02)

    stats = pstats.Stats(str(directory / "backend.prof"))
    assert stats.total_calls > 0  # type: ignore[attr-defined]
    assert (directory / "backend-blocked.log").read_text() == ""
