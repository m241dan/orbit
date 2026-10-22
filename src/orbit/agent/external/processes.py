"""Running a program to completion, and never leaving it, or what it started, behind."""

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

# How long a stopped program's process group has to exit after SIGTERM before it's SIGKILLed.
STOP_GRACE_SECONDS = 2.0


@contextlib.asynccontextmanager
async def spawned(
    command: Sequence[str], **options: Any
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Start ``command`` in a session of its own, passing ``options`` to
    ``asyncio.create_subprocess_exec``, and yield it. If the block raises (is cancelled, say),
    its whole process group is stopped, even if the program itself has exited and only what it
    started is left, and the program is reaped before this exits. If the block ends normally
    with the program still running, the same happens.

    Raises ``OSError`` (such as ``FileNotFoundError``) when the program can't be started.
    """
    process = await asyncio.create_subprocess_exec(*command, start_new_session=True, **options)
    try:
        yield process
    except BaseException:
        # The program may have exited while what it started lives on, holding its pipes open.
        await _stop_group(process)
        raise
    if process.returncode is None:
        await _stop_group(process)


async def _stop_group(process: asyncio.subprocess.Process) -> None:
    """Ask ``process``'s group to stop, kill it after a grace period, and reap ``process``."""
    _signal_group(process.pid, signal.SIGTERM)
    try:
        # wait() also waits for the pipes to close, which a lingering grandchild holds open.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
    finally:
        _signal_group(process.pid, signal.SIGKILL)
        await process.wait()


def _signal_group(group: int, signal_number: int) -> None:
    """Send ``signal_number`` to process group ``group``, if it still has any processes."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signal_number)


async def run_process(
    command: Sequence[str],
    cwd: Path | str,
    *,
    stderr: int = asyncio.subprocess.STDOUT,
    env: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    """Run ``command`` in ``cwd`` with no input, in this process's environment or ``env``;
    returns its exit status and its stdout (with stderr, by default). If cancelled, the
    process and those it started are stopped before this returns.

    Raises ``OSError`` (such as ``FileNotFoundError``) when the program can't be started.
    """
    async with spawned(
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    ) as process:
        output, _ = await process.communicate()
    assert process.returncode is not None
    return process.returncode, output


def process_alive(pid: int) -> bool:
    """Whether the process ``pid`` is running as this user."""
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):  # Gone, another user's, or no process id at all.
        return False
    return True
