"""Running a program to completion, and never leaving it behind."""

import asyncio
from collections.abc import Sequence
from pathlib import Path


async def run_process(
    command: Sequence[str], cwd: Path | str, *, stderr: int = asyncio.subprocess.STDOUT
) -> tuple[int, bytes]:
    """Run ``command`` in ``cwd`` with no input; returns its exit status and its stdout
    (with stderr, by default). If cancelled, the process is killed before this returns.

    Raises ``OSError`` (such as ``FileNotFoundError``) when the program can't be started.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
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
