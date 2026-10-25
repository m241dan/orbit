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
from orbit.agent.application.role_copies import RoleCopies
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import Role, check_model, disabled_note
from orbit.agent.domain.runtimes import not_running
from orbit.agent.domain.sessions import (
    SavedConversation,
    runtime_changed,
    runtime_changed_message,
)
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
    Event,
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
    :class:`Refused` while it's halted, the ``tracing`` its turns are traced by, and its
    ``copies``, which a role working in a copy takes its turns in."""

    save: Callable[[], Awaitable[None]]
    use: Callable[[], None]
    step_names: Callable[[], tuple[str, ...]]
    check: Callable[[], None] = _admit
    tracing: PlannerTracing = _UNTRACED
    copies: RoleCopies | None = None


class SeatLanes:
    """The turn slots of the seats' lanes, posting through ``post``: one for each lane, shared
    by every seat whose turns run in it, so a lane runs one turn at a time."""

    def __init__(self, post: Callable[[Event], None]) -> None:
        self._post = post
        self._slots: dict[BusyLane, TurnSlot] = {}

    def slot(self, lane: BusyLane) -> TurnSlot:
        """Lane ``lane``'s turn slot, made when first asked for."""
        if lane not in self._slots:
            self._slots[lane] = TurnSlot(lane, f"The {lane} lane is busy.", self._post)
        return self._slots[lane]


@dataclass(frozen=True)
class SeatParts:
    """What a seat works with: the ``desk`` running the session's agents, if it has any, the
    ``tasks`` its role proposes into, the ``events`` it publishes into, the ``hooks`` it asks
    its session by, and the ``lanes`` whose slots it shares with the other seats; without, its
    lane's slot is its own."""

    desk: AgentDesk | None
    tasks: Tasks
    events: EventSink
    hooks: SeatHooks
    lanes: SeatLanes | None = None


class PlannerSeat:
    """The planner of a session with the ``parts``' desk's agents (none without them, without a
    planner role or while it's disabled), proposing into their tasks and publishing into their
    events. Given another conversational ``role``, it's that role's seat, its turns in that
    role's lane, as the planner's are in its role's, or in ``planner`` without one, one at a
    time with those of the other seats in the lane. A conversation opened from another runtime
    than its role runs on isn't carried on: it refuses the planner's turns until a reset."""

    def __init__(self, parts: SeatParts, role: str = PLANNER) -> None:
        self._desk = parts.desk
        self._tasks = parts.tasks
        self._events = parts.events
        self._hooks = parts.hooks
        self._copies = parts.hooks.copies
        self._name = role
        self._planner = self._chat()
        self._retired: list[PlannerChat] = []  # Planners of sessions left or reset, still to close.
        # The conversation opened, kept to save without a planner, or while it's stale.
        self._kept: SavedConversation | None = None
        self._kept_notes: tuple[str, ...] = ()  # Its notes, kept to save without a planner.
        # The conversation opened from another runtime than its role's, until a reset.
        self._stale: SavedConversation | None = None
        found = self._role()
        self._lane: BusyLane = found.lane if found else PLANNER_LANE
        lanes = parts.lanes or SeatLanes(parts.events.put_nowait)
        self._turns = lanes.slot(self._lane)  # The lane's, which other seats may share.
        self._refusal = f"The {role} is already answering."
        self._turn: asyncio.Task[object] | None = None

    @property
    def lane(self) -> BusyLane:
        """The lane the seat's turns run in."""
        return self._lane

    @property
    def disabled(self) -> bool:
        """Whether the seat's role is disabled, which refuses its work."""
        role = self._role()
        return role is not None and not role.enabled

    @property
    def running(self) -> bool:
        """Whether the planner is answering."""
        return self._turn is not None and not self._turn.done()

    @property
    def turn(self) -> asyncio.Task[object] | None:
        """The planner's current or last turn; none before the first."""
        return self._turn

    @property
    def conversation(self) -> SavedConversation | None:
        """The planner's conversation, to save, with the runtime it runs on; without a planner,
        or while the one opened is from another runtime than its role's, the one opened."""
        if self._planner is None or self._stale is not None:
            return self._kept
        handle = self._planner.conversation_id
        if handle is None:
            return None
        return SavedConversation(runtime=self._planner.runtime, handle=handle)

    @property
    def notes(self) -> tuple[str, ...]:
        """What the planner is still to hear, to save; without a planner, the notes opened."""
        return self._planner.notes if self._planner else self._kept_notes

    def _chat(self) -> PlannerChat | None:
        role = self._role()
        if self._desk is None or role is None or not role.enabled:
            return None
        chat = PlannerChat(self._desk, self._tasks, self._hooks.step_names, self._name)
        chat.copies = self._copies
        return chat

    def _role(self) -> Role | None:
        """The seat's role; none without agents or that role."""
        return self._desk.agents.roles.get(self._name) if self._desk else None

    def _absent(self) -> Refused:
        """The refusal of the seat's work while it has no planner: its role is disabled, or
        there are no agents or role."""
        message = disabled_note(self._name) if self.disabled else NO_PLANNER_MESSAGE
        return Refused(NOT_CONFIGURED, message)

    def tell(self, note: str) -> None:
        """Have the planner, if there is one, hear ``note`` ahead of its next prompt."""
        if self._planner is not None:
            self._planner.tell(note)

    async def submit(self, text: str, settings: Settings) -> None:
        """Start a turn of the planner's conversation on ``text`` with ``settings``, in its
        own lane, traced as a child of the span current, the command's asking for it; refused
        without a planner, while the conversation opened is from another runtime than its
        role's, while a role in its lane answers, naming that role, or once saving the session
        halted it."""
        if self._planner is None:
            await self._free_prompt()
            raise self._absent()
        if self._stale is not None:
            await self._free_prompt()
            raise self._moved(self._stale, self._planner.runtime)
        self._hooks.use()
        await self._hooks.save()
        self._hooks.check()  # The save may have halted the session.
        work = self._answer(self._planner, text, settings, current_span())
        self._turn = await self._turns.start(work, self._refusal)

    async def _free_prompt(self) -> None:
        """Free the prompt of a frontend that marked the seat's lane busy as it sent a message
        refused, unless another seat's turn holds the lane."""
        if not self._turns.running:
            await self._events.put(BusyChanged(busy=False, lane=self.lane))

    def _moved(self, stale: SavedConversation, runtime: str) -> Refused:
        """The refusal of the seat's turns while the conversation opened, ``stale``, is from
        another runtime than ``runtime``, the one its role runs on."""
        return Refused(INVALID, runtime_changed_message(self._name, stale.runtime, runtime))

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
        """Cancel the planner's turn, if it runs, leaving another seat's in its lane."""
        if self.running:
            self._turns.cancel()

    def replace(self, conversation: SavedConversation | None, notes: tuple[str, ...] = ()) -> None:
        """Have a new planner, carrying on ``conversation`` if there is one and still to hear
        ``notes``; the old one is closed by :meth:`close_retired`, once the switch is done. A
        conversation from another runtime than its role's is kept, to save, but not carried
        on."""
        if self._planner is not None:
            self._retired.append(self._planner)
        self._planner = self._chat()
        self._kept, self._kept_notes = conversation, notes
        self._stale = None
        if self._planner is not None:
            self._carry_on(self._planner, conversation, notes)

    def _carry_on(
        self, planner: PlannerChat, conversation: SavedConversation | None, notes: tuple[str, ...]
    ) -> None:
        """Have ``planner`` carry on ``conversation``, if there is one, unless it's from another
        runtime than its role's, and hear ``notes``."""
        if runtime_changed(conversation, planner.runtime):
            self._stale = conversation
        elif conversation is not None:
            planner.restore(conversation.handle)
        for note in notes:
            planner.tell(note)

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
        """End the planner's conversation, or drop the one opened from another runtime than its
        role's, re-reading its role, and remove its copy, if it has one; refused while it
        answers, and noted when it has no conversation yet. Another role's seat keeps its
        role."""
        if self._desk is None or self._planner is None:
            raise self._absent()
        if self.running:
            raise Refused(BUSY, busy_reset(self._name))
        if not self._has_conversation(self._planner):
            await self._events.put(Notice(message=nothing_to_reset(self._name)))
        else:
            await self._reread_role(self._desk)
            self.replace(None)
            await self.close_retired()
            if self._copies is not None:
                await self._copies.drop(self._name)
            await self._events.put(ConversationReset(agent=self._name))
            await self._hooks.save()

    def _has_conversation(self, planner: PlannerChat) -> bool:
        """Whether there's a conversation to reset: ``planner``'s, or one opened from another
        runtime than its role's."""
        return planner.started or self._stale is not None

    async def _reread_role(self, desk: AgentDesk) -> None:
        """Have the planner's role as the project's config says now, if it can be read again
        and this is its seat; an invalid config, a role on a runtime with no launcher in this
        session, or one whose model the models agents launch with don't resolve, keeps the role
        it has, saying why."""
        read = desk.agents.planner_role
        if read is None or self._name != PLANNER:
            return
        role = read()
        if isinstance(role, ConfigError):
            await self._events.put(ErrorOccurred(code=INVALID, message=f"{ROLE_KEPT}: {role}"))
        elif (problem := _unusable(desk, role)) is not None:
            await self._events.put(ErrorOccurred(code=INVALID, message=f"{ROLE_KEPT}: {problem}"))
        else:
            desk.use_role(PLANNER, role)

    async def stop_turn(self) -> None:
        """Cancel the planner's turn, if it runs, and wait for it to end."""
        if self.running:
            await self._turns.close()

    async def close(self) -> None:
        """End the current planner's conversation, at the session's end."""
        if self._planner is not None:
            await self._planner.close()


def _unusable(desk: AgentDesk, role: Role) -> str | None:
    """Why the planner can't take ``role`` in this session: its runtime has no launcher, or the
    models agents launch with, read at startup, don't resolve its model; none if it can."""
    if role.runtime not in desk.agents.launchers:
        return not_running(role.runtime)
    try:
        check_model(PLANNER, role.model, desk.agents.models.get(role.runtime))
    except Refused as refusal:
        return refusal.message
    return None
