"""Verification steps as processes: how to run one."""

import os

from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.processes import run_process, start_failure

# Steps' output is quoted to agents, so colour codes are noise: NO_COLOR (no-color.org) asks
# tools to leave them out.
STEP_ENV = {"NO_COLOR": "1"}


async def run_step(step: VerificationStep, root: str) -> tuple[bool, str]:
    """Run ``step`` in ``root``; returns whether it passed and its combined stdout and stderr,
    or a line saying so when it failed without any."""
    try:
        returncode, output = await run_process(step.command, root, env={**os.environ, **STEP_ENV})
    except OSError as error:
        return False, start_failure(step.command, root, error)
    text = output.decode(errors="replace")
    if returncode != 0 and not text.strip():
        text = f"No output; exit status {returncode}."
    return returncode == 0, text
