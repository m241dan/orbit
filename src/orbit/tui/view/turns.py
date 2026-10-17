"""The conversation's turns: what each running turn said, when turns and agents started, and the
rule each agent's last turn drew; and what a turn ending or an agent exiting draws from them."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from orbit.protocol import (
    AgentExited,
    AgentStarted,
    AssistantMessage,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from orbit.tui.view.transcript import DrawnRule, TurnStep, final_answer

type TurnKey = tuple[str | None, str]
"""A turn by its agent (None for the planner) and id: turn ids needn't be unique across
agents."""


@dataclass(frozen=True)
class Turns:
    """The ``steps`` of each running turn and when it ``started``, by :data:`TurnKey`; when each
    running agent's last turn, or the agent if no turn has, started (``agent_since``); and the
    rule ``drawn`` for each running agent's last turn, until another starts."""

    steps: Mapping[TurnKey, tuple[TurnStep, ...]] = field(default_factory=dict)
    started: Mapping[TurnKey, float] = field(default_factory=dict)
    agent_since: Mapping[str, float] = field(default_factory=dict)
    drawn: Mapping[str, DrawnRule] = field(default_factory=dict)


@dataclass(frozen=True)
class FinishedTurn:
    """What a turn ending draws: its ``answer``, by the places of its final-answer messages
    among its messages, and the rule's ``elapsed`` seconds since it started, if known."""

    answer: tuple[int, ...]
    elapsed: float | None


@dataclass(frozen=True)
class ExitedAgent:
    """What an agent exiting draws: the ``answers`` of the turns it left open, by turn id, the
    rule ``drawn`` for its last turn, if any, and ``elapsed`` seconds since its last turn (or
    it) started, if known."""

    answers: tuple[tuple[str, tuple[int, ...]], ...]
    drawn: DrawnRule | None
    elapsed: float | None


def agent_started(turns: Turns, event: AgentStarted, now: float) -> Turns:
    """``turns`` with ``event``'s agent started at ``now``."""
    return replace(turns, agent_since={**turns.agent_since, event.agent_id: now})


def turn_started(turns: Turns, event: TurnStarted, now: float) -> Turns:
    """``turns`` with ``event``'s turn started at ``now``: its agent's last turn, whose rule
    no longer ends the agent's run."""
    key = (event.agent_id, event.turn_id)
    turns = replace(turns, started={**turns.started, key: now})
    agent = event.agent_id
    if agent is None:
        return turns
    return replace(
        turns,
        agent_since={**turns.agent_since, agent: now},
        drawn=_without(turns.drawn, agent),
    )


def turn_output(turns: Turns, event: AssistantMessage | ToolCallStarted) -> Turns:
    """``turns`` with ``event`` noted as a step of its turn: a message, or a tool call's
    start."""
    key = (event.agent_id, event.turn_id)
    step: TurnStep = "message" if isinstance(event, AssistantMessage) else "tool"
    return replace(turns, steps={**turns.steps, key: (*turns.steps.get(key, ()), step)})


def turn_finished(turns: Turns, event: TurnFinished, now: float) -> tuple[Turns, FinishedTurn]:
    """``turns`` without ``event``'s turn, which drew its rule at ``now``, and what it
    draws."""
    key = (event.agent_id, event.turn_id)
    started = turns.started.get(key)
    finished = FinishedTurn(
        final_answer(turns.steps.get(key, ())), None if started is None else now - started
    )
    turns = replace(turns, steps=_without(turns.steps, key), started=_without(turns.started, key))
    if event.agent_id is not None:
        drawn = DrawnRule(event, finished.elapsed)
        turns = replace(turns, drawn={**turns.drawn, event.agent_id: drawn})
    return turns, finished


def agent_exited(turns: Turns, event: AgentExited, now: float) -> tuple[Turns, ExitedAgent]:
    """``turns`` without ``event``'s agent, which exited at ``now``, and what its exit
    draws: a stopped or crashed agent sends no TurnFinished for the turns it left open."""
    agent = event.agent_id
    since = turns.agent_since.get(agent)
    exited = ExitedAgent(
        answers=_open_answers(turns.steps, agent),
        drawn=turns.drawn.get(agent),
        elapsed=None if since is None else now - since,
    )
    turns = replace(
        turns,
        steps={key: steps for key, steps in turns.steps.items() if key[0] != agent},
        agent_since=_without(turns.agent_since, agent),
        drawn=_without(turns.drawn, agent),
    )
    return turns, exited


def _open_answers(
    steps: Mapping[TurnKey, tuple[TurnStep, ...]], agent: str
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """The final answers of ``agent``'s turns in ``steps``, by turn id."""
    return tuple(
        (turn, final_answer(said)) for (owner, turn), said in steps.items() if owner == agent
    )


def _without[K, V](mapping: Mapping[K, V], key: K) -> dict[K, V]:
    return {other: value for other, value in mapping.items() if other != key}
