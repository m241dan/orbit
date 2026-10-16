import sys

from orbit.agent.application.verifier import first_failure
from orbit.agent.domain.roles import Setup, setup_step
from orbit.agent.domain.verification import (
    CUT,
    HEAD_LIMIT,
    OUTPUT_LIMIT,
    FallbackFixes,
    VerificationStep,
    check_reply,
    fix_prompt,
    may_change_files,
    skip_reason,
    startup_fix_prompt,
    steps_note,
    trim_output,
)
from orbit.protocol import Settings, StepStatus, TaskSpec


def python_step(code: str, name: str = "check") -> VerificationStep:
    return VerificationStep(name=name, command=(sys.executable, "-c", code))


def task(tool_access: str = "restricted") -> TaskSpec:
    return TaskSpec(prompt="go", settings=Settings(tool_access=tool_access))  # type: ignore[arg-type]


def test_only_tasks_with_tools_can_change_files() -> None:
    assert may_change_files(task("restricted"))
    assert may_change_files(task("full"))
    assert not may_change_files(task("off"))


def test_tasks_with_tools_off_skip_verification() -> None:
    assert skip_reason(task("off"), None, None) == "tools_off"


def test_tasks_that_leave_the_tree_alone_skip_verification() -> None:
    assert skip_reason(task(), "tree", "tree") == "no_changes"


def test_tasks_that_change_the_tree_are_verified() -> None:
    assert skip_reason(task(), "before", "after") is None


def test_tasks_are_verified_when_the_tree_cannot_be_read() -> None:
    assert skip_reason(task(), None, None) is None


def test_fallback_fixes_follow_the_chain_per_step() -> None:
    attempts = FallbackFixes()
    assert not attempts.exhausted("1", "lint", 1)
    attempts.record("2", "1", "lint")
    # The fix carries its chain's count; the task the user queued keeps none.
    assert attempts.exhausted("2", "lint", 1)
    assert not attempts.exhausted("1", "lint", 1)
    # Other steps have their own counts.
    assert not attempts.exhausted("2", "types", 1)
    attempts.record("3", "2", "types")
    assert attempts.exhausted("3", "lint", 1)
    assert attempts.exhausted("3", "types", 1)
    assert not attempts.exhausted("3", "lint", 2)


def test_no_fixes_are_allowed_with_a_limit_of_zero() -> None:
    assert FallbackFixes().exhausted("1", "lint", 0)


def test_fix_prompt_names_the_step_command_and_output() -> None:
    step = VerificationStep(name="Unit tests", command=("uv", "run", "pytest", "-k", "a b"))
    prompt = fix_prompt(step, "FAILED test_x\n")

    first, *rest = prompt.splitlines()
    assert first == "Fix the failed verification step: Unit tests"
    body = "\n".join(rest)
    assert "uv run pytest -k 'a b'" in body
    assert "FAILED test_x" in body
    assert "Do not weaken the check" in body


def test_fix_prompt_keeps_the_start_and_end_of_long_output() -> None:
    output = "HEAD" + "x" * OUTPUT_LIMIT + "TAIL"
    prompt = fix_prompt(python_step("", name="Unit tests"), output)
    kept = "HEAD" + "x" * (HEAD_LIMIT - 4) + CUT + "x" * (OUTPUT_LIMIT - HEAD_LIMIT - 4) + "TAIL"
    assert kept in prompt
    assert "x" * (OUTPUT_LIMIT - 6) not in prompt


def test_short_output_is_quoted_whole_without_surrounding_whitespace() -> None:
    assert trim_output("  \nFAILED\n\n") == "FAILED"
    assert trim_output("x" * OUTPUT_LIMIT) == "x" * OUTPUT_LIMIT


def test_a_failed_check_replies_with_its_trimmed_output() -> None:
    step = VerificationStep(name="Unit tests", command=("pytest",))
    assert check_reply(step, False, " 1 failed\n") == "Unit tests: failed\n\n1 failed"


def test_a_passed_check_replies_with_only_its_last_line() -> None:
    step = VerificationStep(name="Unit tests", command=("pytest",))
    output = "collected 3 items\n\n" + "." * 50 + "\n\n3 passed in 0.1s\n"
    assert check_reply(step, True, output) == "Unit tests: passed (3 passed in 0.1s)"
    assert check_reply(step, True, "\n") == "Unit tests: passed"


def test_startup_fix_prompt_is_titled_by_the_step_and_quotes_its_output() -> None:
    step = VerificationStep(name="Lint", command=("ruff", "check"))
    prompt = startup_fix_prompt(step, "E501 line too long\n")

    first, *rest = prompt.splitlines()
    assert first == "Fix failing check: Lint"
    body = "\n".join(rest)
    assert "at startup" in body
    assert "ruff check" in body
    assert "E501 line too long" in body
    assert "Do not weaken the check" in body


async def test_first_failure_runs_steps_in_order_until_one_fails() -> None:
    steps = (python_step("", "a"), python_step("", "b"), python_step("", "c"))
    ran: list[str] = []
    progress: list[tuple[int, StepStatus]] = []

    async def run_step(step: VerificationStep, root: str) -> tuple[bool, str]:
        ran.append(f"{step.name}@{root}")
        return step.name != "b", f"{step.name} output"

    async def on_step(number: int, status: StepStatus) -> None:
        progress.append((number, status))

    assert await first_failure(steps, run_step, "here", on_step) == (steps[1], "b output")
    assert ran == ["a@here", "b@here"]
    assert progress == [(0, "running"), (0, "passed"), (1, "running"), (1, "failed")]


async def test_first_failure_is_none_when_every_step_passes() -> None:
    async def run_step(step: VerificationStep, root: str) -> tuple[bool, str]:
        return True, ""

    assert await first_failure((python_step(""),), run_step, ".") is None
    assert await first_failure((), run_step, ".") is None


def test_a_setup_runs_as_a_step_named_setup() -> None:
    assert setup_step(Setup(command=("make",))) == VerificationStep("setup", ("make",))


def test_the_steps_note_lists_the_steps_each_task_must_pass() -> None:
    note = steps_note(("Ruff lint", "Unit tests"))
    assert note.endswith("with these checks, in order: Ruff lint, Unit tests.")
    assert "must" not in note  # How to work is the planner's instructions, not the note.


def test_the_steps_note_says_when_nothing_verifies_tasks() -> None:
    assert "No verification steps" in steps_note(())
