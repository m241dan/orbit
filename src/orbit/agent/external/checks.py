"""Verification steps as processes: how to run one."""

import os

from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.processes import run_process

# Steps' output is quoted to agents, so colour codes are noise: NO_COLOR (no-color.org) asks
# tools to leave them out.
STEP_ENV = {"NO_COLOR": "1"}


async def run_step(step: VerificationStep, root: str = ".") -> tuple[bool, str]:
    """Run ``step`` in ``root``; returns whether it passed and its combined stdout and stderr,
    or a line saying so when it failed without any."""
    try:
        returncode, output = await run_process(step.command, root, env={**os.environ, **STEP_ENV})
    except FileNotFoundError:
        return False, f"Could not run {step.command[0]!r}: command not found."
    except OSError as exc:
        return False, f"Could not run {step.command[0]!r}: {exc.strerror}."
    text = output.decode(errors="replace")
    if returncode != 0 and not text.strip():
        text = f"No output; exit status {returncode}."
    return returncode == 0, text
