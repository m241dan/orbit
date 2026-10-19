"""Notes on a task's run: what each reads as, and which tab it goes under."""

from typing import assert_never

from orbit.protocol import (
    SkipReason,
    StepState,
    TaskMerged,
    TaskStarted,
    TaskState,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui.view.common import EntryKind
from orbit.tui.view.tabs import task_tab_key
from orbit.tui.view.transcript import note_entry

SKIP_REASONS: dict[SkipReason, str] = {
    "tools_off": "tools were off",
    "no_changes": "no files changed",
    "unchanged": "unchanged since the last failed check",
    "no_steps": "no verification steps configured",
}

TaskProgress = (
    TaskStarted | VerificationStarted | VerificationSkipped | VerificationFinished | TaskMerged
)
"""Events noted in the transcript as a task runs: its header, verification results and merge."""


def task_note(
    event: TaskProgress,
    state: TaskState | None,
    number: str | None,
    steps: tuple[StepState, ...],
) -> tuple[tuple[str, EntryKind], str | None]:
    """``event``'s transcript entry and the tab key it goes under, besides All.

    ``state`` is the event's task and ``number`` the number the list shows for it, both None
    when the task isn't on the list (removed meanwhile, or the startup check's): it's then
    headed by its id and noted under All only. ``steps`` are the verification steps the
    event's step indexes.
    """
    return progress_entry(event, state, number, steps), task_tab_key(state)


def progress_entry(
    event: TaskProgress,
    state: TaskState | None,
    number: str | None,
    steps: tuple[StepState, ...],
) -> tuple[str, EntryKind]:
    """``event`` as a transcript entry; see ``task_note`` for the other parameters."""
    if isinstance(event, TaskStarted):
        return task_started_entry(event.task_id if number is None else number, state)
    if isinstance(event, TaskMerged):
        return note_entry(event)
    return verification_entry(event, steps)


def verification_entry(
    event: VerificationStarted | VerificationSkipped | VerificationFinished,
    steps: tuple[StepState, ...],
) -> tuple[str, EntryKind]:
    """A verification step starting or finishing, or verification skipped, as a transcript
    entry; ``steps`` are the steps the event's step indexes."""
    match event:
        case VerificationStarted(step=step):
            return verification_started_entry(step_name(steps, step))
        case VerificationSkipped(reason=reason):
            return verification_skipped_entry(reason)
        case VerificationFinished(step=step, passed=passed):
            return verification_finished_entry(step_name(steps, step), passed)
        case _:
            assert_never(event)


def step_name(steps: tuple[StepState, ...], step: int) -> str:
    """The name of step ``step`` of ``steps``, or its number when ``steps`` don't hold it, as
    when a session's log is replayed with other steps shown, or none."""
    if step < len(steps):
        return steps[step].name
    return f"step {step + 1}"


def task_started_entry(number: str, state: TaskState | None) -> tuple[str, EntryKind]:
    """A task's header: its ``number`` and prompt; a task that isn't on the list (``state``
    None) is headed by ``number`` alone, which is then its id."""
    if state is None:
        return f"Task {number}", "user"
    return f"Task {number}: {state.task.prompt}", "user"


def verification_started_entry(name: str) -> tuple[str, EntryKind]:
    return f"🔍 Verifying: {name}…", "verify"


def verification_skipped_entry(reason: SkipReason) -> tuple[str, EntryKind]:
    return f"⏩ Verification skipped: {SKIP_REASONS[reason]}", "verify"


def verification_finished_entry(name: str, passed: bool) -> tuple[str, EntryKind]:
    if passed:
        return f"✅ Verification passed: {name}", "verify"
    return f"❌ Verification failed: {name}", "error"
