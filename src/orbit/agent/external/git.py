"""Running git, one way: what it exits with and prints, or :class:`GitUnavailable` when it
can't be started at all (no git, or a directory to run it in that isn't there or can't be
entered)."""

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orbit.agent.application.ports import TreeFailed
from orbit.agent.external.processes import spawned


class GitUnavailable(TreeFailed):
    """Git couldn't be started."""


@dataclass(frozen=True)
class GitResult:
    """What a git command exited with and printed: ``output`` to stdout, ``errors`` to stderr."""

    status: int
    output: bytes
    errors: bytes = b""

    @property
    def ok(self) -> bool:
        """Whether it succeeded."""
        return self.status == 0

    @property
    def text(self) -> str:
        """What it printed to stdout, decoded and stripped."""
        return self.output.decode(errors="replace").strip()

    @property
    def message(self) -> str:
        """What it printed to stderr, then to stdout, decoded and stripped: git's message."""
        errors = self.errors.decode(errors="replace").strip()
        return "\n".join(part for part in (errors, self.text) if part)


async def git(cwd: Path | str, *args: str, stderr: bool = True) -> GitResult:
    """Run ``git args`` in ``cwd``; returns what it exited with and printed, its stderr apart
    unless ``stderr`` is false, which drops it. If cancelled, git is killed before this returns.

    Raises :class:`GitUnavailable` when git can't be started.
    """
    try:
        async with spawned(
            ("git", *args),
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if stderr else asyncio.subprocess.DEVNULL,
        ) as process:
            output, errors = await process.communicate()
    except OSError as error:
        raise _unavailable(cwd, args, error) from error
    assert process.returncode is not None
    return GitResult(process.returncode, output, errors or b"")


def git_sync(cwd: Path | str, *args: str, stderr: bool = True) -> GitResult:
    """:func:`git`, blocking, for callers that aren't async."""
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if stderr else subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise _unavailable(cwd, args, error) from error
    return GitResult(result.returncode, result.stdout, result.stderr or b"")


def _unavailable(cwd: Path | str, args: tuple[str, ...], error: OSError) -> GitUnavailable:
    """The error for ``git args`` failing to start in ``cwd`` with ``error``."""
    return GitUnavailable(f"git {args[0]} failed to start in {cwd}: {error}")
