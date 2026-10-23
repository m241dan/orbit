import asyncio
import sys
from pathlib import Path

from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.checks import run_step


def python_step(code: str, name: str = "check") -> VerificationStep:
    return VerificationStep(name=name, command=(sys.executable, "-c", code))


async def test_step_passes_on_zero_exit_and_captures_both_streams() -> None:
    step = python_step("import sys; print('out'); print('err', file=sys.stderr)")
    passed, output = await run_step(step, ".")
    assert passed
    assert sorted(output.split()) == ["err", "out"]


async def test_step_fails_on_nonzero_exit() -> None:
    passed, output = await run_step(python_step("import sys; print('3 failed'); sys.exit(1)"), ".")
    assert not passed
    assert output.strip() == "3 failed"


async def test_a_failure_without_output_says_so_with_the_exit_status() -> None:
    passed, output = await run_step(python_step("import sys; sys.exit(2)"), ".")
    assert not passed
    assert output == "No output; exit status 2."


async def test_steps_run_with_colour_turned_off() -> None:
    passed, output = await run_step(python_step("import os; print(os.environ['NO_COLOR'])"), ".")
    assert passed
    assert output.strip() == "1"


async def test_missing_command_fails_the_step() -> None:
    step = VerificationStep(name="check", command=("orbit-no-such-command",))
    passed, output = await run_step(step, ".")
    assert not passed
    assert output == "Could not run 'orbit-no-such-command': command not found."


async def test_non_executable_command_fails_the_step(tmp_path: Path) -> None:
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o644)
    step = VerificationStep(name="check", command=(str(script),))
    passed, output = await run_step(step, ".")
    assert not passed
    assert output == f"Could not run {str(script)!r}: Permission denied."


async def test_cancelling_a_step_kills_its_process() -> None:
    marker = "import time; time.sleep(60)"
    task = asyncio.create_task(run_step(python_step(marker), "."))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
    assert task.cancelled()


async def test_step_runs_in_the_directory_it_is_given(tmp_path: Path) -> None:
    passed, output = await run_step(python_step("import os; print(os.getcwd())"), str(tmp_path))
    assert passed
    assert output.strip() == str(tmp_path)
