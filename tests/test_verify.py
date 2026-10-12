import sys

from orbit.agent.domain.verification import (
    OUTPUT_LIMIT,
    FixAttempts,
    VerificationStep,
    fix_prompt,
    may_change_files,
    skip_reason,
)
from orbit.protocol import Settings, TaskSpec


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


def test_fix_attempts_follow_the_chain_per_step() -> None:
    attempts = FixAttempts()
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
    assert FixAttempts().exhausted("1", "lint", 0)


def test_fix_prompt_names_the_step_command_and_output() -> None:
    step = VerificationStep(name="Unit tests", command=("uv", "run", "pytest", "-k", "a b"))
    prompt = fix_prompt(step, "FAILED test_x\n")

    first, *rest = prompt.splitlines()
    assert first == "Fix the failed verification step: Unit tests"
    body = "\n".join(rest)
    assert "uv run pytest -k 'a b'" in body
    assert "FAILED test_x" in body
    assert "Do not weaken the check" in body


def test_fix_prompt_keeps_only_the_end_of_long_output() -> None:
    output = "x" * OUTPUT_LIMIT + "TAIL"
    prompt = fix_prompt(python_step("", name="Unit tests"), output)
    assert "TAIL" in prompt
    assert "x" * OUTPUT_LIMIT not in prompt
    assert "…" + "x" * (OUTPUT_LIMIT - 4) + "TAIL" in prompt
