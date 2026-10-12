import asyncio
import sys

from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.checks import DEFAULT_STEPS, run_step


def python_step(code: str, name: str = "check") -> VerificationStep:
    return VerificationStep(name=name, command=(sys.executable, "-c", code))


def test_default_steps_run_the_quick_checks_before_the_tests() -> None:
    assert [(s.name, s.command) for s in DEFAULT_STEPS] == [
        ("Ruff lint", ("uv", "run", "ruff", "check")),
        ("Ruff format", ("uv", "run", "ruff", "format", "--check")),
        ("Type check (mypy)", ("uv", "run", "mypy")),
        ("Import contracts", ("uv", "run", "lint-imports")),
        (
            "Code quality (Lizard)",
            ("uv", "run", "lizard", "src", "tests", "-w", "-C", "6", "-L", "100", "-a", "5"),
        ),
        ("Unit tests, 100% coverage", ("uv", "run", "pytest", "--cov", "--cov-fail-under=100")),
    ]


async def test_step_passes_on_zero_exit_and_captures_both_streams() -> None:
    step = python_step("import sys; print('out'); print('err', file=sys.stderr)")
    passed, output = await run_step(step)
    assert passed
    assert sorted(output.split()) == ["err", "out"]


async def test_step_fails_on_nonzero_exit() -> None:
    passed, output = await run_step(python_step("import sys; print('3 failed'); sys.exit(1)"))
    assert not passed
    assert output.strip() == "3 failed"


async def test_missing_command_fails_the_step() -> None:
    step = VerificationStep(name="check", command=("orbit-no-such-command",))
    passed, output = await run_step(step)
    assert not passed
    assert output == "Could not run 'orbit-no-such-command': command not found."


async def test_cancelling_a_step_kills_its_process() -> None:
    marker = "import time; time.sleep(60)"
    task = asyncio.create_task(run_step(python_step(marker)))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()
