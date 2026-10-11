"""Verification steps: checks run after each task, and the prompt that asks to fix a failure."""

import asyncio
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationStep:
    """A command run in the working directory; the step passes when it exits with status 0."""

    name: str
    command: tuple[str, ...]


DEFAULT_STEPS = (
    VerificationStep(
        name="Unit tests, 100% coverage",
        command=("uv", "run", "pytest", "--cov", "--cov-fail-under=100"),
    ),
)
# Fix tasks queued in a row for one failing task before giving up.
MAX_FIX_ATTEMPTS = 3
# Characters of step output quoted in a fix prompt; the end (summary, coverage) matters most.
OUTPUT_LIMIT = 8000


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


def fix_prompt(step: VerificationStep, output: str) -> str:
    """A task prompt asking to fix why ``step`` failed. Its first line works as a title."""
    output = output.strip()
    if len(output) > OUTPUT_LIMIT:
        output = "…" + output[-OUTPUT_LIMIT:]
    return (
        f"Fix the failed verification step: {step.name}\n\n"
        f"This command must exit successfully:\n\n    {shlex.join(step.command)}\n\n"
        f"It failed. The end of its output:\n\n{output}\n\n"
        "Find and fix the cause so the command passes. Do not weaken the check: don't"
        " lower thresholds, skip or delete tests, or exclude code from coverage."
    )
