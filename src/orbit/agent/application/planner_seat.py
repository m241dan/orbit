"""The planner's seat in a session, or another conversational role's: its current conversation,
those a switch or reset retired, and the lane its turns run in."""

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
from orbit.agent.domain.roles import Role, disabled_note
from orbit.protocol import (
    BUSY,
    INVALID,
    NOT_CONFIGURED,
    PLANNER,
    PLANNER_LANE,
    BusyChanged,
    BusyLane,
    ConversationReset,
    ErrorOccurred,
    Notice,
    Settings,
    TurnEndReason,
)
from orbit.tracing import Span, current_span

NO_PLANNER_MESSAGE = "Planning needs agents and a planner role."
ROLE_KEPT = "Kept the planner's role, as the config is invalid"


def busy_reset(role: str) -> str:
    """Why the conversation of the conversational role ``role`` can't be reset as it answers."""
    return f"The {role} is answering; press ctrl+x to stop it first."


def nothing_to_reset(role: str) -> str:
    """Why the conversational role ``role`` has nothing to reset before its first turn."""
    return f"The {role} has no conversation to reset."


PLANNER_BUSY_RESET = busy_reset(PLANNER)
NOTHING_TO_RESET = nothing_to_reset(PLANNER)


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


@dataclass(frozen=True)
class SeatParts:
    """What a seat works with: the ``desk`` running the session's agents, if it has any, the
    ``tasks`` its role proposes into, the ``events`` it publishes into, and the ``hooks`` it
    asks its session by."""

    desk: AgentDesk | None
    tasks: Tasks
    events: EventSink
    hooks: SeatHooks


class PlannerSeat:
    """The planner of a session with the ``parts``' desk's agents (none without them, without a
    planner role or while it's disabled), proposing into their tasks and publishing into their
    events. Given another conversational ``role``, it's that role's seat, its turns in that
    role's lane, as the planner's are in its role's, or in ``planner`` without one."""

    def __init__(self, parts: SeatParts, role: str = PLANNER) -> None:
        self._desk = parts.desk
        self._tasks = parts.tasks
        self._events = parts.events
        self._hooks = parts.hooks
        self._name = role
        self._planner = self._chat()
        self._retired: list[PlannerChat] = []  # Planners of sessions left or reset, still to close.
        self._kept: str | None = None  # The conversation opened, kept to save without a planner.
        self._kept_notes: tuple[str, ...] = ()  # Its notes, kept the same way.
        found = self._role()
        self._lane: BusyLane = found.lane if found else PLANNER_LANE
        refusal = f"The {role} is already answering."
        self._turns = TurnSlot(self._lane, refusal, parts.events.put_nowait)

    @property
    def lane(self) -> BusyLane:
        """The lane the seat's turns run in."""
        return self._lane

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
        role = self._role()
        if self._desk is None or role is None or not role.enabled:
            return None
        return PlannerChat(self._desk, self._tasks, self._hooks.step_names, self._name)

    def _role(self) -> Role | None:
        """The seat's role; none without agents or that role."""
        return self._desk.agents.roles.get(self._name) if self._desk else None

    def _absent(self) -> Refused:
        """The refusal of the seat's work while it has no planner: its role is disabled, or
        there are no agents or role."""
        role = self._role()
        disabled = role is not None and not role.enabled
        message = disabled_note(self._name) if disabled else NO_PLANNER_MESSAGE
        return Refused(NOT_CONFIGURED, message)

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
            await self._events.put(BusyChanged(busy=False, lane=self.lane))
            raise self._absent()
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
        noted when it has no conversation yet. Another role's seat keeps its role."""
        if self._desk is None or self._planner is None:
            raise self._absent()
        if self._turns.running:
            raise Refused(BUSY, busy_reset(self._name))
        if not self._planner.started:
            await self._events.put(Notice(message=nothing_to_reset(self._name)))
        else:
            await self._reread_role(self._desk)
            self.replace(None)
            await self.close_retired()
            await self._events.put(ConversationReset(agent=self._name))
            await self._hooks.save()

    async def _reread_role(self, desk: AgentDesk) -> None:
        """Have the planner's role as the project's config says now, if it can be read again
        and this is its seat; an invalid config keeps the role it has, saying why."""
        read = desk.agents.planner_role
        if read is None or self._name != PLANNER:
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
