"""Running a program to completion, and never leaving it behind."""

import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path


async def run_process(
    command: Sequence[str],
    cwd: Path | str,
    *,
    stderr: int = asyncio.subprocess.STDOUT,
    env: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    """Run ``command`` in ``cwd`` with no input, in this process's environment or ``env``;
    returns its exit status and its stdout (with stderr, by default). If cancelled, the
    process is killed before this returns.

    Raises ``OSError`` (such as ``FileNotFoundError``) when the program can't be started.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    )
    try:
        output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None
    return process.returncode, output


def process_alive(pid: int) -> bool:
    """Whether the process ``pid`` is running as this user."""
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):  # Gone, another user's, or no process id at all.
        return False
    return True
