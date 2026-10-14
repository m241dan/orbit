"""Verification rules: when a task needs checking, how many fixes to try, and what to ask for."""

import shlex
from dataclasses import dataclass

from orbit.protocol import SkipReason, TaskSpec


@dataclass(frozen=True)
class VerificationStep:
    """A command run in the working directory; the step passes when it exits with status 0."""

    name: str
    command: tuple[str, ...]


# Fix tasks one task's chain of fixes may queue for each step before giving up on it.
MAX_FIX_ATTEMPTS = 3
# Characters of step output quoted in a fix prompt; the end (summary, coverage) matters most.
OUTPUT_LIMIT = 8000


def may_change_files(task: TaskSpec) -> bool:
    """Whether ``task`` can touch the working tree at all; with tools off it can't."""
    return task.settings.tool_access != "off"


def skip_reason(task: TaskSpec, before: object, after: object) -> SkipReason | None:
    """Why ``task``, whose turn completed, needs no verification; ``None`` if it does.

    ``before`` and ``after`` fingerprint the working tree around the task. ``before`` is
    ``None`` when that couldn't be told, so the task counts as having changed files.
    """
    if not may_change_files(task):
        return "tools_off"
    if before is not None and before == after:
        return "no_changes"
    return None


class FixAttempts:
    """The fixes queued for each step, per task, counted along each chain of fixes.

    Tasks the user queued have none. A fix inherits the counts of the task it fixes, plus
    one for the step it fixes, so a chain's attempts at each step are counted separately.
    """

    def __init__(self) -> None:
        self._made: dict[str, dict[str, int]] = {}

    def exhausted(self, task_id: str, step: str, limit: int) -> bool:
        """Whether the chain leading to task ``task_id`` has queued ``limit`` fixes for ``step``."""
        return self._made.get(task_id, {}).get(step, 0) >= limit

    def record(self, fix_id: str, task_id: str, step: str) -> None:
        """Task ``fix_id`` was queued to fix ``step`` after task ``task_id``."""
        made = self._made.get(task_id, {})
        self._made[fix_id] = {**made, step: made.get(step, 0) + 1}


def trim_output(output: str) -> str:
    """``output`` without surrounding whitespace, keeping only its end if it's long."""
    output = output.strip()
    return "…" + output[-OUTPUT_LIMIT:] if len(output) > OUTPUT_LIMIT else output


def fix_prompt(step: VerificationStep, output: str) -> str:
    """A task prompt asking to fix why ``step`` failed. Its first line works as a title."""
    output = trim_output(output)
    return (
        f"Fix the failed verification step: {step.name}\n\n"
        f"This command must exit successfully:\n\n    {shlex.join(step.command)}\n\n"
        f"It failed. The end of its output:\n\n{output}\n\n"
        "Find and fix the cause so the command passes. Do not weaken the check: don't"
        " lower thresholds, skip or delete tests, or exclude code from coverage."
    )
