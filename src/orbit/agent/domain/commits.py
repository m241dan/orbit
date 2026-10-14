"""Commit messages for the work agents finish."""

import shlex
from collections.abc import Sequence

from orbit.agent.domain.roles import ChecksProposal
from orbit.protocol import TaskSpec

# Git's conventional limit for a subject line, and how much of the task to quote below it.
SUBJECT_LIMIT = 72
PROMPT_LIMIT = 2000


def commit_message(task: TaskSpec, summaries: Sequence[str]) -> str:
    """The agent's first summary as the subject (or the task's first line without one), the
    task's prompt as the body, and any later summaries, of fixes made after verification
    failed, listed below it."""
    message = (
        f"{_subject(task, summaries)}\n\nOrbit task:\n\n{_trim(task.prompt.strip(), PROMPT_LIMIT)}"
    )
    fixes = [line for line in map(_first_line, summaries[1:]) if line]
    if fixes:
        message += "\n\nFixed after verification:\n" + "\n".join(f"- {fix}" for fix in fixes)
    return message


def _subject(task: TaskSpec, summaries: Sequence[str]) -> str:
    first = _first_line(summaries[0]) if summaries else ""
    return _trim(first or _first_line(task.prompt), SUBJECT_LIMIT - 1)


def _trim(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()


def configure_message(checks: ChecksProposal) -> str:
    """The message committing accepted ``checks``: each step listed, then the setup."""
    lines = [f"- {step.name}: {shlex.join(step.command)}" for step in checks.steps]
    message = "Configure verification steps\n\nSteps:\n" + "\n".join(lines)
    if checks.setup is not None:
        message += f"\n\nSetup: {shlex.join(checks.setup.command)}"
        if checks.setup.watch:
            message += f" (again when {', '.join(checks.setup.watch)} changes)"
    return message
