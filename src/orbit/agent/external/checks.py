"""Verification steps as processes: how to run one."""

from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.processes import run_process


async def run_step(step: VerificationStep, root: str = ".") -> tuple[bool, str]:
    """Run ``step`` in ``root``; returns whether it passed and its combined stdout and stderr."""
    try:
        returncode, output = await run_process(step.command, root)
    except FileNotFoundError:
        return False, f"Could not run {step.command[0]!r}: command not found."
    except OSError as exc:
        return False, f"Could not run {step.command[0]!r}: {exc.strerror}."
    return returncode == 0, output.decode(errors="replace")
