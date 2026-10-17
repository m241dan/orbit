"""The rules ending turns and agents' runs: what each reads as, when an agent's exit redraws
the rule of its last turn, and which of a turn's messages are its final answer."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from orbit.protocol import AgentExited, AgentExitReason, TurnEndReason, TurnFinished, Usage
from orbit.tui.view.common import EntryKind, token_text

TURN_END_LABELS: dict[TurnEndReason, str] = {
    "completed": "done",
    "cancelled": "stopped",
    "max_steps": "out of steps",
    "error": "error",
}


def duration_text(seconds: float) -> str:
    """``seconds`` as a turn's time reads: ``42s``, ``1m12s``, or ``1h03m`` from an hour."""
    whole = int(seconds)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def usage_text(usage: Usage) -> str:
    """The tokens a turn read (``↑``, its input with what was cached and read from the cache)
    and wrote (``↓``)."""
    return f"↑{token_text(usage.read_tokens)} ↓{token_text(usage.output_tokens)}"


def _turn_cost(usage: Usage | None, elapsed: float | None) -> list[str]:
    """How long a turn took, the ``usage``'s time else ``elapsed`` seconds if known, and its
    tokens when it has ``usage``."""
    if usage is None:
        return [] if elapsed is None else [duration_text(elapsed)]
    return [duration_text(usage.duration_ms / 1000), usage_text(usage)]


def turn_rule_entry(
    event: TurnFinished, elapsed: float | None, speaker: str | None
) -> tuple[str, EntryKind]:
    """The rule ending ``event``'s turn: how it ended, headed by its ``speaker`` if any, then
    how long it took (the usage's time, else ``elapsed`` seconds, if known) and its tokens when
    it has usage. A turn ending in an error is a rule of its own kind."""
    label = TURN_END_LABELS[event.reason]
    return _rule(label, speaker, _turn_cost(event.usage, elapsed), event.reason == "error")


def _rule(label: str, speaker: str | None, cost: list[str], error: bool) -> tuple[str, EntryKind]:
    """A rule reading ``label``, headed by its ``speaker`` if any, then its ``cost``; an
    ``error`` rule is of its own kind."""
    parts = [label if speaker is None else f"{speaker} {label}", *cost]
    return " · ".join(parts), "error_rule" if error else "rule"


EXIT_LABELS: dict[AgentExitReason, str] = {
    "finished": "done",
    "killed": "stopped",
    "crashed": "crashed",
}


@dataclass(frozen=True)
class DrawnRule:
    """The rule drawn for an agent's last turn: the ``turn``'s end, ``elapsed`` seconds after
    it started, if known."""

    turn: TurnFinished
    elapsed: float | None


@dataclass(frozen=True)
class ExitRule:
    """The rule an agent's exit draws: its ``text`` and ``kind``, either replacing the rule of
    the agent's last turn (``replaces``) or added after it."""

    text: str
    kind: EntryKind
    replaces: bool


def exit_rule(
    event: AgentExited, drawn: DrawnRule | None, elapsed: float | None, speaker: str | None
) -> ExitRule | None:
    """What ``event``'s agent exiting does to the rules ending its run, so the run ends in
    one: with a rule ``drawn`` for its last turn, a ``done`` one turns ``stopped`` or
    ``crashed`` when the agent was killed or crashed, and is left as it is otherwise (None).
    With none, a rule of its own says how the agent exited, headed by its ``speaker`` if any,
    ``elapsed`` seconds after its last turn (or the agent) started, if known."""
    label = EXIT_LABELS[event.reason]
    crashed = event.reason == "crashed"
    if drawn is None:
        cost = [] if elapsed is None else [duration_text(elapsed)]
        return ExitRule(*_rule(label, speaker, cost, crashed), replaces=False)
    if event.reason == "finished" or drawn.turn.reason != "completed":
        return None
    cost = _turn_cost(drawn.turn.usage, drawn.elapsed)
    return ExitRule(*_rule(label, speaker, cost, crashed), replaces=True)


TurnStep = Literal["message", "tool"]
"""What a turn's output was, in order: a message, or the start of a tool call."""


def final_answer(steps: Sequence[TurnStep]) -> tuple[int, ...]:
    """Which of a turn's messages are its final answer, by their place among the turn's
    messages: those after its last tool call, given the turn's ``steps``. A turn that ends on a
    tool call has none."""
    last_tool = max((index for index, step in enumerate(steps) if step == "tool"), default=-1)
    trailing = len(steps) - last_tool - 1
    total = steps.count("message")
    return tuple(range(total - trailing, total))
