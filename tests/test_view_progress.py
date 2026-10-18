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
from orbit.tui.view.progress import (
    task_note,
    task_started_entry,
    verification_finished_entry,
    verification_skipped_entry,
    verification_started_entry,
)

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


def test_task_progress_entries() -> None:
    state = TaskState(id="7", task=TaskSpec(prompt="do it"))
    assert task_started_entry("1.2", state) == ("Task 1.2: do it", "user")
    # A task that isn't on the list has no prompt to show, only its id.
    assert task_started_entry("9", None) == ("Task 9", "user")
    step = StepState(name="Lint")
    assert verification_started_entry(step) == ("🔍 Verifying: Lint…", "verify")
    assert verification_skipped_entry("no_changes") == (
        "⏩ Verification skipped: no files changed",
        "verify",
    )
    assert verification_finished_entry(step, True) == ("✅ Verification passed: Lint", "verify")
    assert verification_finished_entry(step, False) == ("❌ Verification failed: Lint", "error")


def test_no_steps_skip_reason_reads_naturally() -> None:
    text, kind = verification_skipped_entry("no_steps")
    assert text == "⏩ Verification skipped: no verification steps configured"
    assert kind == "verify"


def test_unchanged_skip_reason_reads_naturally() -> None:
    text, kind = verification_skipped_entry("unchanged")
    assert "unchanged" in text and kind == "verify"
