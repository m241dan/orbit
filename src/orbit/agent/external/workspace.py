"""Whether a task changed any files: a fingerprint of the working tree to compare."""

import asyncio
import os
from pathlib import Path

from orbit.agent.external.git import GitUnavailable, git

WorkspaceState = frozenset[tuple[str, int, int]]


async def workspace_state(root: Path | str = ".") -> WorkspaceState | None:
    """The files git sees under ``root`` (tracked, plus untracked ones not ignored), each
    with its size and modification time; a missing tracked file has both as -1.

    ``None`` when ``root`` isn't in a git work tree or git can't be run, so the caller
    can't tell whether anything changed.
    """
    try:
        result = await git(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", stderr=False
        )
    except GitUnavailable:
        return None
    if not result.ok:
        return None
    paths = {p for p in result.output.decode(errors="surrogateescape").split("\0") if p}
    return await asyncio.to_thread(_fingerprints, Path(root), paths)


def _fingerprints(root: Path, paths: set[str]) -> WorkspaceState:
    """Each of ``paths`` under ``root`` with its size and modification time."""
    return frozenset(_fingerprint(root, path) for path in paths)


def _fingerprint(root: Path, path: str) -> tuple[str, int, int]:
    try:
        stat = os.stat(root / path)
    except FileNotFoundError:
        return path, -1, -1
    return path, stat.st_size, stat.st_mtime_ns
