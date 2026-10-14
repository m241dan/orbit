"""Commit messages for the work agents finish."""

from orbit.protocol import TaskSpec

# Git's conventional limit for a subject line, and how much of the task to quote below it.
SUBJECT_LIMIT = 72
PROMPT_LIMIT = 2000


def commit_message(task: TaskSpec, summary: str | None) -> str:
    """The agent's summary as the subject (or the task's first line without one), and the
    task's prompt as the body."""
    subject = _first_line(summary or "") or _first_line(task.prompt)
    if len(subject) > SUBJECT_LIMIT:
        subject = subject[: SUBJECT_LIMIT - 1] + "…"
    prompt = task.prompt.strip()
    if len(prompt) > PROMPT_LIMIT:
        prompt = prompt[:PROMPT_LIMIT] + "…"
    return f"{subject}\n\nOrbit task:\n\n{prompt}"


def _first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()
