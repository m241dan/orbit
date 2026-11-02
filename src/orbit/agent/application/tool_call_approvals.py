"""The agents' calls to MCP tools held for the user's approval, as their servers' ``approve``
says, each until the user allows or declines it, it expires, declined, after an hour with no
frontend connected, or it's withdrawn as the agent's run ends.

The calls are numbered in the session from a high-water mark saved with it, as consultations
are, so a number is never used twice in a session. Each settlement is published to every
frontend, as is each call held, and the calls still held are in the session's snapshot.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from orbit.agent.application.consult_approvals import Approvals, Held, Timer
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    UNKNOWN,
    DecideToolCall,
    Event,
    ToolCallDecided,
    ToolCallDecision,
    ToolCallHeld,
)

type Answer = asyncio.Future[ToolCallDecision]
"""What a held call is settled with, once it is."""

_LOOP_TIMER = Timer()


@dataclass(frozen=True)
class ToolCall:
    """An agent's call that waits for the user's approval: agent ``agent_id``, in the role
    ``role``, calling the tool ``tool`` of ``server``, by orbit's name for it, with
    ``arguments``."""

    agent_id: str
    role: str
    server: str
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Ruling:
    """How a held call was settled, ``decision``, once it had been held for ``held``."""

    decision: ToolCallDecision
    held: timedelta


def call_of(request: ToolCallHeld) -> int:
    """The number of the call ``request`` holds."""
    return request.call


class ToolCallApprovals:
    """The calls held for approval, each published through ``post`` as it's held and as it's
    settled, the session saved through ``save`` as each is numbered; timed by ``timer``, each
    still held after an hour with no frontend connected expires. Until told otherwise, no
    frontend is connected."""

    def __init__(
        self,
        post: Callable[[Event], None],
        save: Callable[[], Awaitable[None]],
        timer: Timer = _LOOP_TIMER,
    ) -> None:
        self._post = post
        self._save = save
        self._clock = timer.clock
        self._approvals = Approvals[ToolCallHeld, Answer](post, self._expired, timer, call_of)
        self._last = 0

    @property
    def waiting(self) -> tuple[ToolCallHeld, ...]:
        """The calls held, in the order they were held."""
        return self._approvals.waiting

    @property
    def last(self) -> int:
        """The number of the last call held in the session, to save."""
        return self._last

    async def ask(self, call: ToolCall, noted: Callable[[Ruling], None]) -> ToolCallDecision:
        """How ``call``, held until it's settled, was settled, ``noted`` as it is. Cancelled
        while held, as the agent's run ends, it's withdrawn, which is noted, and the
        cancellation goes on."""
        self._last += 1
        request = ToolCallHeld(
            call=self._last,
            agent_id=call.agent_id,
            role=call.role,
            server=call.server,
            tool=call.tool,
            arguments=dict(call.arguments),
        )
        answer: Answer = asyncio.get_running_loop().create_future()
        started = self._clock()
        self._approvals.hold(request, answer)
        decision: ToolCallDecision = "withdrawn"
        try:
            await self._save()
            decision = await answer
        finally:
            self._settle(request.call, "withdrawn")  # Unless it was settled already.
            noted(Ruling(decision, timedelta(seconds=self._clock() - started)))
        return decision

    def decide(self, command: DecideToolCall) -> None:
        """Settle the call ``command`` decides on as allowed or declined, and publish that;
        refused for one that isn't held."""
        decision: ToolCallDecision = "allowed" if command.decision == "allow" else "declined"
        if not self._settle(command.call, decision):
            raise Refused(UNKNOWN, f"No tool call {command.call} is waiting for approval.")

    def decline_all(self) -> None:
        """Settle every call held as declined, and publish that, as the session is left or orbit
        closes."""
        for held in self._approvals.take_where(lambda _: True):
            self._answer(held, "declined")

    def replace(self, last: int) -> None:
        """Number the next call held after ``last``, as another session opens, declining any
        still held."""
        self.decline_all()
        self._last = last

    def viewers(self, count: int) -> None:
        """Note that ``count`` frontends view the session, as held calls expire only with
        none."""
        self._approvals.viewers(count)

    def pause(self) -> None:
        """Stop timing the calls held, until :meth:`unpause`, as the session halts."""
        self._approvals.pause()

    def unpause(self) -> None:
        """Time the calls held to expire again, as a halt lifts."""
        self._approvals.unpause()

    def _settle(self, number: int, decision: ToolCallDecision) -> bool:
        """Whether call ``number`` was held, settling it with ``decision`` if so."""
        held = self._approvals.take(number)
        if held is not None:
            self._answer(held, decision)
        return held is not None

    def _answer(self, held: Held[ToolCallHeld, Answer], decision: ToolCallDecision) -> None:
        """Publish that ``held`` was settled with ``decision``, and answer it so, unless its
        caller stopped waiting."""
        self._post(ToolCallDecided(call=held.request.call, decision=decision))
        if not held.work.done():
            held.work.set_result(decision)

    async def _expired(self, held: Held[ToolCallHeld, Answer]) -> None:
        """Settle ``held``, held for an hour with no frontend connected, as expired."""
        self._answer(held, "expired")
