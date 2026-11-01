"""Verification rules: when a task needs checking, how many fixes to try, what to ask for, and
what a named set's failure says."""

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Literal

from pydantic import AfterValidator, Field
from pydantic_core import PydanticCustomError

from orbit.agent.domain.roles import PLAIN_NAME
from orbit.protocol import TASK_CHECKS, SkipReason, StepState, StepStatus, TaskSpec


def _set_name(name: str) -> str:
    """A set's ``name``, refused unless it's a plain name other than :data:`TASK_CHECKS`."""
    if not PLAIN_NAME.fullmatch(name):
        raise PydanticCustomError(
            "set_name", "a set's name is a plain name: letters, digits, - and _"
        )
    if name == TASK_CHECKS:
        raise PydanticCustomError(
            "set_name", f"{TASK_CHECKS} names the steps in no set; a set takes another name."
        )
    return name


SetName = Annotated[str, AfterValidator(_set_name)]
"""A set's name, a plain name as a trigger's is, since its settings are keyed by it between dots,
as in ``verification.sets.slow.holds_merges``."""


@dataclass(frozen=True)
class VerificationStep:
    """A command run in the working directory; the step passes when it exits with status 0. In
    no ``set``, it's one of the task checks every task runs."""

    name: Annotated[str, Field(description="What the step is called; no two steps share a name.")]
    command: Annotated[
        tuple[str, ...],
        Field(description="The command run in the working directory; it passes if it exits 0."),
    ]
    set: Annotated[
        SetName | None,
        Field(
            description=(
                "The named set it's in; left out, it's one of the task checks every task runs."
            )
        ),
    ] = None


SetEffect = Literal["holds_merges", "reports"]
"""What a named set's failure does: hold merges until a fix lands, or only report."""


@dataclass(frozen=True)
class CheckSet:
    """A named set of verification steps, as a trigger runs it: its ``name``, its ``steps``, in
    order, and what its failure does, its ``effect``."""

    name: str
    steps: tuple[VerificationStep, ...]
    effect: SetEffect = "reports"


def set_failed_note(name: str, step: str) -> str:
    """The notice saying a run of the reporting set ``name`` failed at the step named ``step``,
    pointing at the verification panel, which shows its run."""
    return f"{name} reported: {step} failed (see the verification panel)"


NOTE_TAIL_LINES = 10
"""How many of a failing step's output's last lines a notice quotes, as many as the panel shows."""


def set_failed_away_note(name: str, step: str, output: str) -> str:
    """The notice saying a run of the reporting set ``name`` failed at the step named ``step``,
    once a task's verification took the panel from it: quoting the last lines of the ``output``
    it failed with, trimmed, since the panel doesn't show them."""
    tail = "\n".join(trim_output(output).splitlines()[-NOTE_TAIL_LINES:])
    if not tail:
        return f"{name} reported: {step} failed, with no output"
    return f"{name} reported: {step} failed. Its output ended:\n\n{tail}"


@dataclass(frozen=True)
class StepProgress:
    """How far a run of the verification steps has got: each step's status, in order."""

    steps: tuple[StepState, ...] = ()

    @classmethod
    def of(cls, names: Iterable[str]) -> StepProgress:
        """Progress over the steps ``names``, none of them run yet."""
        return cls(tuple(StepState(name=name) for name in names))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    def set(self, step: int, status: StepStatus) -> StepProgress:
        """This progress with step number ``step`` at ``status``."""
        steps = list(self.steps)
        steps[step] = steps[step].model_copy(update={"status": status})
        return replace(self, steps=tuple(steps))

    def reset(self) -> StepProgress:
        """This progress with every step pending again."""
        return StepProgress.of(self.names)

    def skip(self) -> StepProgress:
        """This progress with every step skipped."""
        return replace(
            self, steps=tuple(s.model_copy(update={"status": "skipped"}) for s in self.steps)
        )

    def cancel_running(self) -> StepProgress:
        """This progress with the step running, if any, cancelled."""
        return replace(
            self,
            steps=tuple(
                s.model_copy(update={"status": "cancelled"}) if s.status == "running" else s
                for s in self.steps
            ),
        )


# Fix tasks one task's chain of fixes may queue for each step before giving up on it.
MAX_FALLBACK_FIXES = 3
# Characters of step output quoted in a fix prompt, and how many of them come from its start:
# the first failure is usually there, and the summary at the end.
OUTPUT_LIMIT = 8000
HEAD_LIMIT = 2000
CUT = "\n\n[… output cut …]\n\n"


def may_change_files(task: TaskSpec) -> bool:
    """Whether ``task`` can touch the working tree at all; with tools off it can't."""
    return task.settings.tool_access != "off"


def skip_reason(
    task: TaskSpec, before: object, after: object, *, fixing: bool = False, has_steps: bool = True
) -> SkipReason | None:
    """Why ``task``, whose turn completed, needs no verification; ``None`` if it does.

    ``before`` and ``after`` fingerprint the working tree around the task. ``before`` is
    ``None`` when that couldn't be told, so the task counts as having changed files. A task
    ``fixing`` another is verified even if it changed nothing, as passing completes that one.
    Without ``has_steps`` there's nothing to verify with, unless another reason skips it first.
    """
    if not may_change_files(task):
        return "tools_off"
    if before is not None and before == after and not fixing:
        return "no_changes"
    if not has_steps:
        return "no_steps"
    return None


class FallbackFixes:
    """The fixes queued for each step, per task, counted along each chain of fixes.

    Tasks the user queued have none. A fix inherits the counts of the task it fixes, plus
    one for the step it fixes, so a chain's attempts at each step are counted separately.
    """

    def __init__(self) -> None:
        self._made: dict[str, dict[str, int]] = {}

    def exhausted(self, task_id: str, step: str, limit: int) -> bool:
        """Whether the chain leading to task ``task_id`` has queued ``limit`` fixes for ``step``."""
        return self._made.get(task_id, {}).get(step, 0) >= limit

    def record(self, fix_id: str, task_id: str, step: str) -> None:
        """Task ``fix_id`` was queued to fix ``step`` after task ``task_id``."""
        made = self._made.get(task_id, {})
        self._made[fix_id] = {**made, step: made.get(step, 0) + 1}


def trim_output(output: str) -> str:
    """``output`` without surrounding whitespace, keeping only its start and end if it's long."""
    output = output.strip()
    if len(output) <= OUTPUT_LIMIT:
        return output
    return output[:HEAD_LIMIT] + CUT + output[-(OUTPUT_LIMIT - HEAD_LIMIT) :]


def failure_report(step: VerificationStep, output: str) -> str:
    """What ``step`` runs, and the ``output`` it failed with, trimmed. The command is in the
    first paragraph, so a fix task's commit message can quote just that."""
    return (
        f"This command failed and must pass:\n    {shlex.join(step.command)}\n\n"
        f"Its output:\n\n{trim_output(output)}"
    )


def failure_reason(step: VerificationStep, output: str, rebased: bool = False) -> tuple[str, str]:
    """Why a task ended unverified because ``step`` failed with ``output``, in a line and in
    full; ``rebased`` when it failed rechecking the work after a rebase."""
    reason = f"{step.name} failed" + (" after rebasing" if rebased else "")
    return reason, failure_report(step, output)


def check_reply(step: VerificationStep, passed: bool, output: str) -> str:
    """The reply to an agent that ran ``step`` itself: the trimmed ``output`` if it failed, else
    just that it ``passed``, with the output's last line (usually a summary) if there is one."""
    if not passed:
        return f"{step.name}: failed\n\n{trim_output(output)}"
    last = output.strip().rsplit("\n", 1)[-1].strip()
    return f"{step.name}: passed" + (f" ({last})" if last else "")


FIX_INSTRUCTIONS = (
    "Find and fix the cause so the command passes, leaving the check as strong as it was: the"
    " same thresholds, the same tests, the same code under coverage."
)


def fix_prompt(step: VerificationStep, output: str) -> str:
    """A task prompt asking to fix why ``step`` failed. Its first line works as a title."""
    return (
        f"Fix the failed verification step: {step.name}\n\n"
        f"{failure_report(step, output)}\n\n{FIX_INSTRUCTIONS}"
    )


def startup_fix_prompt(step: VerificationStep, output: str) -> str:
    """A task prompt asking to fix why ``step`` failed on the base commit at startup, before
    any task ran. Its first line works as a title."""
    return (
        f"Fix failing check: {step.name}\n\n"
        "Orbit ran the verification steps on the base commit at startup, and this one"
        f" failed. {failure_report(step, output)}\n\n{FIX_INSTRUCTIONS}"
    )


def step_list(names: Sequence[str]) -> str:
    """The step ``names`` as Markdown code spans, joined with commas, so a name with a comma
    in it still reads as one name."""
    return ", ".join(f"`` {name} ``" if "`" in name else f"`{name}`" for name in names)


def steps_note(steps: Sequence[str]) -> str:
    """What the planner is told about the verification steps named ``steps``."""
    if not steps:
        return "Orbit note: No verification steps are configured yet, so tasks aren't verified."
    return f"Orbit note: orbit verifies each task with these checks, in order: {step_list(steps)}."
