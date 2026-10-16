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


def backend_work() -> None:
    """A function only the backend thread runs."""


async def run_profiled_backend(directory: Path) -> None:
    with profiled(directory, "backend"):
        backend_work()


async def test_profiled_on_a_second_thread_leaves_the_profile_to_the_first(
    tmp_path: Path,
) -> None:
    with profiled(tmp_path, "ui"):
        backend = threading.Thread(target=asyncio.run, args=(run_profiled_backend(tmp_path),))
        backend.start()
        backend.join()

    # The UI's profile already sees every thread, so the backend writes only its log.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "backend-blocked.log",
        "ui-blocked.log",
        "ui.prof",
    ]
    stats = pstats.Stats(str(tmp_path / "ui.prof"))
    assert any(name == "backend_work" for _, _, name in stats.stats)  # type: ignore[attr-defined]


async def test_stopping_from_the_loops_thread_mid_block_returns() -> None:
    log = io.StringIO()
    dog = watchdog(log)
    dog.start()
    await asyncio.sleep(0.05)
    rescued = threading.Event()

    def rescue() -> None:
        # Without stop() releasing the pending check, the watchdog waits forever on a no-op
        # the loop can't run while its thread joins; releasing it here turns that hang into
        # a failure.
        rescued.set()
        dog._pending.set()

    guard = threading.Timer(5, rescue)
    guard.start()
    try:
        hogs_the_loop()
        dog.stop()
    finally:
        guard.cancel()

    assert not rescued.is_set()
    text = log.getvalue()
    assert text.count("blocked for") == 1
    assert text.count("unblocked after") == 1
    assert "ui unblocked after" in text.splitlines()[-1]
