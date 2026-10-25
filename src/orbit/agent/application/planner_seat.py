"""The planner's seat in a session: its current conversation, those a switch or reset retired,
and the lane its turns run in."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orbit.agent.application.closing import close_all
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import TurnSlot
from orbit.agent.application.outbox import EventSink
from orbit.agent.application.planner import PlannerChat
from orbit.agent.application.planner_trace import PlannerTracing
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.refusals import Refused
from orbit.protocol import (
    BUSY,
    INVALID,
    NOT_CONFIGURED,
    PLANNER,
    PLANNER_LANE,
    BusyChanged,
    ConversationReset,
    ErrorOccurred,
    Notice,
    Settings,
    TurnEndReason,
)
from orbit.tracing import Span, current_span

NO_PLANNER_MESSAGE = "Planning needs agents and a planner role."
PLANNER_BUSY_RESET = "The planner is answering; press ctrl+x to stop it first."
NOTHING_TO_RESET = "The planner has no conversation to reset."
ROLE_KEPT = "Kept the planner's role, as the config is invalid"


_UNTRACED = PlannerTracing()


def _admit() -> None:
    """Refuse nothing."""


@dataclass(frozen=True)
class SeatHooks:
    """What the seat asks of its session: to ``save`` it, to mark it ``use``d, the
    ``step_names`` verifying tasks now, to ``check`` it, refusing a turn by raising
    :class:`Refused` while it's halted, and the ``tracing`` its turns are traced by."""

    save: Callable[[], Awaitable[None]]
    use: Callable[[], None]
    step_names: Callable[[], tuple[str, ...]]
    check: Callable[[], None] = _admit
    tracing: PlannerTracing = _UNTRACED


class PlannerSeat:
    """The planner of a session with ``desk``'s agents (none without them, or without a
    planner role), proposing into ``tasks``; it publishes into ``events``."""

    def __init__(
        self,
        desk: AgentDesk | None,
        tasks: Tasks,
        events: EventSink,
        hooks: SeatHooks,
    ) -> None:
        self._desk = desk
        self._tasks = tasks
        self._events = events
        self._hooks = hooks
        self._planner = self._chat()
        self._retired: list[PlannerChat] = []  # Planners of sessions left or reset, still to close.
        self._kept: str | None = None  # The conversation opened, kept to save without a planner.
        self._kept_notes: tuple[str, ...] = ()  # Its notes, kept the same way.
        self._turns = TurnSlot(PLANNER_LANE, "The planner is already answering.", events.put_nowait)

    @property
    def running(self) -> bool:
        """Whether the planner is answering."""
        return self._turns.running

    @property
    def turn(self) -> asyncio.Task[object] | None:
        """The planner's current or last turn; none before the first."""
        return self._turns.turn

    @property
    def conversation_id(self) -> str | None:
        """The planner's conversation, to save; without a planner, the one opened."""
        return self._planner.conversation_id if self._planner else self._kept

    @property
    def notes(self) -> tuple[str, ...]:
        """What the planner is still to hear, to save; without a planner, the notes opened."""
        return self._planner.notes if self._planner else self._kept_notes

    def _chat(self) -> PlannerChat | None:
        if self._desk is None or PLANNER not in self._desk.agents.roles:
            return None
        return PlannerChat(self._desk, self._tasks, self._hooks.step_names)

    def tell(self, note: str) -> None:
        """Have the planner, if there is one, hear ``note`` ahead of its next prompt."""
        if self._planner is not None:
            self._planner.tell(note)

    async def submit(self, text: str, settings: Settings) -> None:
        """Start a turn of the planner's conversation on ``text`` with ``settings``, in its
        own lane, traced as a child of the span current, the command's asking for it; refused
        without a planner, while it answers, or once saving the session halted it."""
        if self._planner is None:
            # Frees the prompt of a frontend that marked the lane busy when it sent this.
            await self._events.put(BusyChanged(busy=False, lane=PLANNER_LANE))
            raise Refused(NOT_CONFIGURED, NO_PLANNER_MESSAGE)
        self._hooks.use()
        await self._hooks.save()
        self._hooks.check()  # The save may have halted the session.
        await self._turns.start(self._answer(self._planner, text, settings, current_span()))

    async def _answer(
        self, planner: PlannerChat, text: str, settings: Settings, parent: Span
    ) -> TurnEndReason:
        """``planner``'s turn on ``text``, in its span, a child of ``parent``, saving the
        session after it, as its conversation may have changed."""
        # The span starts as the turn does, not as it's asked for, so a turn refused, or
        # cancelled before it runs, leaves no span unended.
        with self._hooks.tracing.turn(parent, planner.model(settings)):
            reason = await planner.answer(text, settings)
        await self._hooks.save()
        return reason

    def cancel(self) -> None:
        """Cancel the planner's turn, if it runs."""
        self._turns.cancel()

    def replace(self, conversation: str | None, notes: tuple[str, ...] = ()) -> None:
        """Have a new planner, carrying on ``conversation`` if there is one and still to hear
        ``notes``; the old one is closed by :meth:`close_retired`, once the switch is done."""
        if self._planner is not None:
            self._retired.append(self._planner)
        self._planner = self._chat()
        self._kept, self._kept_notes = conversation, notes
        if self._planner is None:
            return
        if conversation is not None:
            self._planner.restore(conversation)
        for note in notes:
            self._planner.tell(note)

    async def close_retired(self) -> None:
        """Close the planners retired by leaving a session or by a reset, the last retired
        first, each even if closing one before it fails, as :func:`close_all` does; then those
        retired while they closed, round by round until none are left."""
        retired, self._retired = self._retired, []
        if retired:
            # The last stop closes the planners a switch or reset retired during the awaits
            # before it, so close_all collects their failures with the rest.
            await close_all([*(planner.close for planner in reversed(retired)), self.close_retired])

    async def reset(self) -> None:
        """End the planner's conversation, re-reading its role; refused while it answers, and
        noted when it has no conversation yet."""
        if self._desk is None or self._planner is None:
            raise Refused(NOT_CONFIGURED, NO_PLANNER_MESSAGE)
        if self._turns.running:
            raise Refused(BUSY, PLANNER_BUSY_RESET)
        if not self._planner.started:
            await self._events.put(Notice(message=NOTHING_TO_RESET))
        else:
            await self._reread_role(self._desk)
            self.replace(None)
            await self.close_retired()
            await self._events.put(ConversationReset(agent=PLANNER))
            await self._hooks.save()

    async def _reread_role(self, desk: AgentDesk) -> None:
        """Have the planner's role as the project's config says now, if it can be read again;
        an invalid config keeps the role it has, saying why."""
        read = desk.agents.planner_role
        if read is None:
            return
        role = read()
        if isinstance(role, ConfigError):
            await self._events.put(ErrorOccurred(code=INVALID, message=f"{ROLE_KEPT}: {role}"))
        else:
            desk.use_role(PLANNER, role)

    async def stop_turn(self) -> None:
        """Cancel the planner's turn, if it runs, and wait for it to end."""
        await self._turns.close()

    async def close(self) -> None:
        """End the current planner's conversation, at the session's end."""
        if self._planner is not None:
            await self._planner.close()
