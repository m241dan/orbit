"""One agent's work on one task: what orbit replies to it, and when the work gets verified.

The agent asks for its task, works, and claims it's done. Orbit verifies the claim; a failure
goes back to the same agent to fix, up to a limit, after which the task falls back to a fresh
agent. An agent that exits without claiming done is taken to have finished.
"""

from dataclasses import dataclass
from typing import Literal

from orbit.agent.domain.verification import VerificationStep, fix_prompt
from orbit.protocol import TaskSpec

Phase = Literal["assigned", "working", "verifying", "done", "fallback", "gave_up"]
"""Where the agent's work stands.

``assigned``: started, hasn't asked for the task yet. ``working``: has it. ``verifying``: claimed
it's done, and the checks are running. ``done``: the checks passed. ``fallback``: they failed with
no fixes left to ask of this agent, so another one takes over. ``gave_up``: the agent said it can't.
"""

FINISHED: frozenset[Phase] = frozenset({"done", "fallback", "gave_up"})

STOP = "End your turn now."
REFUSALS: dict[Phase, str] = {
    "assigned": "Call get_task first.",
    "verifying": "Orbit is already verifying your work; wait for that reply.",
    "done": "This task is finished. " + STOP,
    "fallback": "This task is finished. " + STOP,
    "gave_up": "This task is finished. " + STOP,
}


@dataclass(frozen=True)
class Limits:
    """How much one agent may ask of orbit on one task.

    ``max_attempts``: fixes asked of it before falling back to a fresh agent.
    ``max_checks``: verification steps it may run itself before claiming done.
    """

    max_attempts: int = 5
    max_checks: int = 20

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_checks < 1:
            raise ValueError("Limits must be at least 1.")


@dataclass(frozen=True)
class Claim:
    """The answer to a claim that the task is done: verify it, or reply with ``reply``."""

    verify: bool
    reply: str | None = None


def task_brief(task: TaskSpec) -> str:
    """The reply to ``get_task``: the task, and how to report back."""
    return (
        f"{task.prompt}\n\n"
        "When you have finished, call complete_task with a one-line summary of what you did;"
        " orbit then verifies your work and replies. If you can't do the task, call give_up"
        " with the reason."
    )


class AgentTask:
    """The state of one agent working on ``task``, driven by what the agent asks for."""

    def __init__(self, task: TaskSpec, limits: Limits) -> None:
        self.task = task
        self.limits = limits
        self.phase: Phase = "assigned"
        self.alive = True
        self.attempt = 0
        self.checks = 0
        self.summaries: list[str] = []  # One per claim orbit accepted, in order.
        self.reason: str | None = None

    def get_task(self) -> str:
        if self.phase == "assigned":
            self.phase = "working"
        return task_brief(self.task)

    def claim_done(self, summary: str) -> Claim:
        if self.phase != "working":
            return Claim(verify=False, reply=REFUSALS[self.phase])
        self.phase = "verifying"
        self.summaries.append(summary)
        return Claim(verify=True)

    @property
    def summary(self) -> str | None:
        """What the agent said it did when it first claimed the task was done."""
        return self.summaries[0] if self.summaries else None

    def passed(self) -> str:
        self.phase = "done"
        return "All verification steps passed. " + STOP

    def failed(self, step: VerificationStep, output: str) -> str:
        """The checks failed at ``step``: ask this agent for a fix, or fall back."""
        if not self.alive or self.attempt >= self.limits.max_attempts:
            self.phase = "fallback"
            return (
                f"Verification still fails at {step.name!r}; another agent will take over. {STOP}"
            )
        self.phase = "working"
        self.attempt += 1
        return (
            f"{fix_prompt(step, output)}\n\nThen call complete_task again."
            f" (Fix {self.attempt} of {self.limits.max_attempts}.)"
        )

    def exited(self) -> bool:
        """The agent's process ended; returns whether its work should now be verified."""
        self.alive = False
        if self.phase != "working":
            return False
        self.phase = "verifying"
        return True

    def give_up(self, reason: str) -> str:
        if self.phase != "working":
            return REFUSALS[self.phase]
        self.phase, self.reason = "gave_up", reason
        return f"Understood; the task is marked as given up. {STOP}"

    def request_check(self) -> str | None:
        """Whether the agent may run a verification step now; ``None`` if so, else why not."""
        if self.phase != "working":
            return REFUSALS[self.phase]
        if self.checks >= self.limits.max_checks:
            return f"You have used all {self.limits.max_checks} checks; call complete_task."
        self.checks += 1
        return None
