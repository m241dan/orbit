"""Verification steps as processes: the checks orbit runs by default, and how to run one."""

import asyncio

from orbit.agent.domain.verification import VerificationStep

# Lizard's limits: cyclomatic complexity, lines per function and parameters per function.
LIZARD_MAX_CCN = 6
LIZARD_MAX_LENGTH = 100
LIZARD_MAX_ARGUMENTS = 5

# Quick checks first, so a failure shows up in seconds rather than after the tests.
DEFAULT_STEPS = (
    VerificationStep(name="Ruff lint", command=("uv", "run", "ruff", "check")),
    VerificationStep(name="Ruff format", command=("uv", "run", "ruff", "format", "--check")),
    VerificationStep(name="Type check (mypy)", command=("uv", "run", "mypy")),
    VerificationStep(name="Import contracts", command=("uv", "run", "lint-imports")),
    VerificationStep(
        name="Code quality (Lizard)",
        command=(
            "uv",
            "run",
            "lizard",
            "src",
            "tests",
            "-w",
            "-C",
            str(LIZARD_MAX_CCN),
            "-L",
            str(LIZARD_MAX_LENGTH),
            "-a",
            str(LIZARD_MAX_ARGUMENTS),
        ),
    ),
    VerificationStep(
        name="Unit tests, 100% coverage",
        command=("uv", "run", "pytest", "--cov", "--cov-fail-under=100"),
    ),
)


async def run_step(step: VerificationStep) -> tuple[bool, str]:
    """Run ``step``; returns whether it passed and its combined stdout and stderr."""
    try:
        process = await asyncio.create_subprocess_exec(
            *step.command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"Could not run {step.command[0]!r}: command not found."
    try:
        output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode == 0, output.decode(errors="replace")
