"""Running git, one way: what it exits with and prints, or :class:`GitUnavailable` when it
can't be started at all (no git, or a directory to run it in that isn't there or can't be
entered)."""

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orbit.agent.application.ports import TreeFailed
from orbit.agent.external.processes import run_process


class GitUnavailable(TreeFailed):
    """Git couldn't be started."""


@dataclass(frozen=True)
class GitResult:
    """What a git command exited with and printed."""

    status: int
    output: bytes

    @property
    def ok(self) -> bool:
        """Whether it succeeded."""
        return self.status == 0

    @property
    def text(self) -> str:
        """What it printed, decoded and stripped."""
        return self.output.decode(errors="replace").strip()


async def git(cwd: Path | str, *args: str, stderr: bool = True) -> GitResult:
    """Run ``git args`` in ``cwd``; returns what it exited with and printed, its stderr too
    unless ``stderr`` is false. If cancelled, git is killed before this returns.

    Raises :class:`GitUnavailable` when git can't be started.
    """
    try:
        status, output = await run_process(
            ("git", *args),
            cwd,
            stderr=asyncio.subprocess.STDOUT if stderr else asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        raise _unavailable(cwd, args, error) from error
    return GitResult(status, output)


def git_sync(cwd: Path | str, *args: str, stderr: bool = True) -> GitResult:
    """:func:`git`, blocking, for callers that aren't async."""
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if stderr else subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise _unavailable(cwd, args, error) from error
    return GitResult(result.returncode, result.stdout)


def _unavailable(cwd: Path | str, args: tuple[str, ...], error: OSError) -> GitUnavailable:
    """The error for ``git args`` failing to start in ``cwd`` with ``error``."""
    return GitUnavailable(f"git {args[0]} failed to start in {cwd}: {error}")
