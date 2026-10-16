from orbit.protocol import (
    StepState,
    TaskMerged,
    TaskSpec,
    TaskStarted,
    TaskState,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)
from orbit.tui.view import task_note

CODER_TASK = TaskState(id="7", task=TaskSpec(prompt="do it"))
STEPS = (StepState(name="Lint"), StepState(name="Tests"))


def test_a_started_task_is_headed_by_its_number_under_its_roles_tab() -> None:
    note = task_note(TaskStarted(task_id="7"), CODER_TASK, "1.2", STEPS)
    assert note == (("Task 1.2: do it", "user"), "coder")


def test_a_task_off_the_list_is_headed_by_its_id_under_all_only() -> None:
    assert task_note(TaskStarted(task_id="9"), None, None, STEPS) == (("Task 9", "user"), None)


def test_a_task_whose_role_has_no_tab_is_noted_under_all_only() -> None:
    state = TaskState(id="7", task=TaskSpec(prompt="do it", role="translator"))
    assert task_note(TaskStarted(task_id="7"), state, "1", STEPS) == (
        ("Task 1: do it", "user"),
        None,
    )


def test_verification_notes_name_the_step_they_index() -> None:
    started = VerificationStarted(task_id="7", step=1)
    assert task_note(started, CODER_TASK, "1", STEPS) == (
        ("🔍 Verifying: Tests…", "verify"),
        "coder",
    )
    failed = VerificationFinished(task_id="7", step=0, passed=False)
    assert task_note(failed, CODER_TASK, "1", STEPS) == (
        ("❌ Verification failed: Lint", "error"),
        "coder",
    )


def test_a_skipped_verification_says_why() -> None:
    skipped = VerificationSkipped(task_id="7", reason="no_changes")
    assert task_note(skipped, None, None, ()) == (
        ("⏩ Verification skipped: no files changed", "verify"),
        None,
    )


def test_a_merge_is_noted_under_the_tasks_tab() -> None:
    merged = TaskMerged(task_id="7", result="merged")
    assert task_note(merged, CODER_TASK, "1", STEPS) == (
        ("⇢ Merged into the base branch.", "verify"),
        "coder",
    )
