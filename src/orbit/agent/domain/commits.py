"""Commit messages for the work agents finish."""

from collections.abc import Sequence

from orbit.agent.domain.checks import ChecksProposal, describe_checks
from orbit.protocol import TaskSpec

# Git's conventional limit for a subject line, and how much of the task to quote below it.
SUBJECT_LIMIT = 72
PROMPT_LIMIT = 2000


def commit_message(task: TaskSpec, summaries: Sequence[str], fix: bool = False) -> str:
    """The agent's first summary as the subject (or the task's first line without one), the
    task's prompt as the body, and any later summaries, of fixes made after verification
    failed, listed below it. A ``fix`` task's prompt quotes a failed step's output, so only
    its lead, the step and its command, goes in the body."""
    prompt = _lead(task.prompt) if fix else task.prompt.strip()
    message = f"{_subject(task, summaries)}\n\nOrbit task:\n\n{_trim(prompt, PROMPT_LIMIT)}"
    fixes = [line for line in map(_first_line, summaries[1:]) if line]
    if fixes:
        message += "\n\nFixed after verification:\n" + "\n".join(f"- {fix}" for fix in fixes)
    return message


def _subject(task: TaskSpec, summaries: Sequence[str]) -> str:
    first = _first_line(summaries[0]) if summaries else ""
    return _trim(first, SUBJECT_LIMIT - 1) if first else task_title(task)


def task_title(task: TaskSpec) -> str:
    """``task``'s title: its prompt's first line, cut to fit a commit's subject."""
    return _trim(_first_line(task.prompt), SUBJECT_LIMIT - 1)


def _trim(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _lead(prompt: str) -> str:
    """``prompt``'s first two paragraphs: a fix prompt's title and the command that failed."""
    return "\n\n".join(prompt.strip().split("\n\n")[:2])


def _first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()


def configure_message(checks: ChecksProposal) -> str:
    """The message committing accepted ``checks``: each step listed, then the setup."""
    return "Configure verification steps\n\n" + describe_checks(checks.steps, checks.setup)
