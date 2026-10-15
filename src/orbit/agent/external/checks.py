"""Verification steps as processes: how to run one."""

import asyncio

from orbit.agent.domain.verification import VerificationStep


async def run_step(step: VerificationStep, root: str = ".") -> tuple[bool, str]:
    """Run ``step`` in ``root``; returns whether it passed and its combined stdout and stderr."""
    try:
        process = await asyncio.create_subprocess_exec(
            *step.command,
            cwd=root,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"Could not run {step.command[0]!r}: command not found."
    except OSError as exc:
        return False, f"Could not run {step.command[0]!r}: {exc.strerror}."
    try:
        output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode == 0, output.decode(errors="replace")
