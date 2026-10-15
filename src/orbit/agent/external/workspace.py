"""Whether a task changed any files: a fingerprint of the working tree to compare."""

import asyncio
import os
from pathlib import Path

from orbit.agent.external.processes import run_process

WorkspaceState = frozenset[tuple[str, int, int]]


async def workspace_state(root: Path | str = ".") -> WorkspaceState | None:
    """The files git sees under ``root`` (tracked, plus untracked ones not ignored), each
    with its size and modification time; a missing tracked file has both as -1.

    ``None`` when ``root`` isn't in a git work tree or git can't be run, so the caller
    can't tell whether anything changed.
    """
    try:
        returncode, output = await run_process(
            ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
            root,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    if returncode != 0:
        return None
    paths = {p for p in output.decode(errors="surrogateescape").split("\0") if p}
    return frozenset(_fingerprint(Path(root), path) for path in paths)


def _fingerprint(root: Path, path: str) -> tuple[str, int, int]:
    try:
        stat = os.stat(root / path)
    except FileNotFoundError:
        return path, -1, -1
    return path, stat.st_size, stat.st_mtime_ns
