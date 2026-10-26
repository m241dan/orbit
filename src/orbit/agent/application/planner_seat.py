"""The planner's seat in a session, or another conversational role's: its current conversation,
those a switch or reset retired, the lane its turns run in, and the turns waiting for it."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from functools import partial

from orbit.agent.application.closing import close_all
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.lanes import TurnSlot
from orbit.agent.application.outbox import EventSink
from orbit.agent.application.planner import Consult, PlannerChat
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


def _default_settings(role: str) -> Settings:
    return Settings()


def _unplaced(position: int) -> None:
    """Note nothing of a waiting turn's place."""


async def _unanswered(reason: TurnEndReason, reply: str, why: str | None) -> None:
    """Do nothing with a turn's reply."""


def _under(span: Span) -> Callable[[], AbstractContextManager[Span]]:
    """What makes ``span``, already started, current for a waiting turn."""
    return lambda: nullcontext(span)


@dataclass(frozen=True)
class WaitingTurn:
    """A turn waiting in a seat's line: on ``text`` with ``settings``, run under the span
    ``opened`` makes current, telling ``answered`` how it ended, its reply and why it gave none,
    if it didn't, under that span still, and ``placed`` its place in line: 1 once it runs next,
    or runs. When ``root``, the turn's span starts its own trace instead, linked to each of
    ``links``."""

    text: str
    settings: Settings
    opened: Callable[[], AbstractContextManager[Span]]
    answered: Callable[[TurnEndReason, str, str | None], Awaitable[None]] = _unanswered
    placed: Callable[[int], None] = _unplaced
    links: tuple[Span, ...] = ()
    root: bool = False


def loop_time() -> float:
    """The running loop's clock."""
    return asyncio.get_running_loop().time()


@dataclass(frozen=True)
class Timer:
    """A ``clock``, in seconds, and what ``sleep``s for so many of them."""

    clock: Callable[[], float] = loop_time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class SeatHooks:
    """What the seat asks of its session: to ``save`` it, to mark it ``use``d, the
    ``step_names`` verifying tasks now, to ``check`` it, refusing a turn by raising
    :class:`Refused` while it's halted, the ``tracing`` its turns are traced by, its
    ``copies``, which a role working in a copy takes its turns in, the ``settings`` of each
    role, which the turns it didn't ask for run with, and the ``timer`` its consultations held
    for approval are timed by."""

    save: Callable[[], Awaitable[None]]
    use: Callable[[], None]
    step_names: Callable[[], tuple[str, ...]]
    check: Callable[[], None] = _admit
    tracing: PlannerTracing = _UNTRACED
    copies: RoleCopies | None = None
    settings: Callable[[str], Settings] = _default_settings
    timer: Timer = field(default_factory=Timer)


def _unwatched(lane: BusyLane) -> None:
    """Note nothing of a lane freed."""


class SeatLanes:
    """The turn slots of the seats' lanes, posting through ``post``: one for each lane, shared
    by every seat whose turns run in it, so a lane runs one turn at a time; ``freed`` is told
    each lane that's free again."""

    def __init__(
        self, post: Callable[[Event], None], freed: Callable[[BusyLane], None] = _unwatched
    ) -> None:
        self._post = post
        self._freed = freed
        self._slots: dict[BusyLane, TurnSlot] = {}

    def slot(self, lane: BusyLane) -> TurnSlot:
        """Lane ``lane``'s turn slot, made when first asked for."""
        if lane not in self._slots:
            slot = TurnSlot(lane, f"The {lane} lane is busy.", self._post)
            slot.freed = partial(self._freed, lane)
            self._slots[lane] = slot
        return self._slots[lane]


@dataclass(frozen=True)
class SeatParts:
    """What a seat works with: the ``desk`` running the session's agents, if it has any, the
    ``tasks`` its role proposes into, the ``events`` it publishes into, the ``hooks`` it asks
    its session by, and the ``lanes`` whose slots it shares with the other seats; without, its
    lane's slot is its own; and how its role ``consult``s others, if it can."""

    desk: AgentDesk | None
    tasks: Tasks
    events: EventSink
    hooks: SeatHooks
    lanes: SeatLanes | None = None
    consult: Consult | None = field(default=None, compare=False)


class PlannerSeat:
    """The planner of a session with the ``parts``' desk's agents (none without them, without a
    planner role or while it's disabled), proposing into their tasks and publishing into their
    events. Given another conversational ``role``, it's that role's seat, its turns in that
    role's lane, as the planner's are in its role's, or in ``planner`` without one, one at a
    time with those of the other seats in the lane. A conversation opened from another runtime
    than its role runs on isn't carried on: it refuses the planner's turns until a reset. Turns
    it didn't ask for, such as consultations, wait in its line, as do the messages sent while
    one runs or waits; the messages delivered to it wait for one turn after the line."""

    def __init__(self, parts: SeatParts, role: str = PLANNER) -> None:
        self._desk = parts.desk
        self._tasks = parts.tasks
        self._events = parts.events
        self._hooks = parts.hooks
        self._copies = parts.hooks.copies
        self._consult = parts.consult
        self._name = role
        self._line: list[WaitingTurn] = []  # The turns waiting to run, in order.
        # The messages waiting to be heard, in one turn, each with the span that caused it.
        self._delivered: list[tuple[str, Span | None]] = []
        self._waited = False  # Whether the turn running, or the last, waited in the line.
        self._held = False  # Whether waiting turns are kept from starting, as turns stop.
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
    def moved(self) -> str | None:
        """Why the seat can't take turns until a reset: its conversation opened is from another
        runtime than its role's; none otherwise."""
        if self._stale is None or self._planner is None:
            return None
        return self._moved(self._stale, self._planner.runtime).message

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
        if self._consult is not None:
            chat.consult = self._consult
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
        if self._waiting():
            self.wait(WaitingTurn(text, settings, _under(current_span())))
            return
        parent = current_span()
        work = self._answer(self._planner, WaitingTurn(text, settings, _under(parent)), parent)
        self._turn = await self._turns.start(work, self._refusal)
        self._waited = False

    @property
    def settings(self) -> Settings:
        """The settings of the seat's role, which the turns it didn't ask for run with."""
        return self._hooks.settings(self._name)

    def _waiting(self) -> bool:
        """Whether a message waits its turn: the seat has turns or messages waiting, or the
        turn running waited in its line."""
        return bool(self._line or self._delivered) or (self.running and self._waited)

    def wait(self, turn: WaitingTurn) -> None:
        """Put ``turn`` at the end of the seat's line, starting it at once if nothing is ahead
        and the seat's lane is free."""
        self._line.append(turn)
        self.advance()
        self._place()

    def deliver(self, message: str, cause: Span | None) -> None:
        """Have the seat's role hear ``message`` in a turn after those in its line, with every
        other message delivered meanwhile, starting it at once if the seat's lane is free; the
        turn's span starts its own trace, linked to ``cause``, if given, and the others'."""
        self._delivered.append((message, cause))
        self.advance()

    def advance(self) -> None:
        """Start the seat's next waiting turn, if it has one and its lane is free: the first in
        its line, else one hearing every message delivered."""
        if self._held or self._planner is None or self._stale or self._turns.running:
            return
        turn = self._next()
        if turn is None:
            return
        work = self._answer_waiting(self._planner, turn)
        self._turn = self._turns.begin(work, self._refusal)
        self._waited = True
        turn.placed(1)
        self._place()

    def hold(self) -> None:
        """Keep the turns waiting from starting, as the seat's turn stops, until
        :meth:`drop_waiting`."""
        self._held = True

    def drop_waiting(self) -> None:
        """Forget the turns and messages waiting, as turns stop, and start those waiting from
        now on again."""
        self._line, self._delivered, self._held = [], [], False

    def _next(self) -> WaitingTurn | None:
        """The next waiting turn, taken from the line, or made of the messages delivered; none
        if nothing waits."""
        if self._line:
            return self._line.pop(0)
        if not self._delivered:
            return None
        delivered, self._delivered = self._delivered, []
        text = "\n\n".join(message for message, _ in delivered)
        causes = tuple(cause for _, cause in delivered if cause is not None)
        return WaitingTurn(text, self.settings, _under(current_span()), links=causes, root=True)

    def _place(self) -> None:
        """Tell each turn in the line its place: after the turn running, if one is."""
        ahead = 1 if self.running else 0
        for position, turn in enumerate(self._line, start=1 + ahead):
            turn.placed(position)

    async def _answer_waiting(self, planner: PlannerChat, turn: WaitingTurn) -> TurnEndReason:
        """``planner``'s ``turn`` that waited in the line, under its span, telling it how it
        ended, what it answered and why it didn't, if it didn't."""
        with turn.opened() as parent:
            reason = await self._answer(planner, turn, parent)
            await turn.answered(reason, planner.reply, planner.interruption(reason))
        return reason

    async def _free_prompt(self) -> None:
        """Free the prompt of a frontend that marked the seat's lane busy as it sent a message
        refused, unless another seat's turn holds the lane."""
        if not self._turns.running:
            await self._events.put(BusyChanged(busy=False, lane=self.lane))

    def _moved(self, stale: SavedConversation, runtime: str) -> Refused:
        """The refusal of the seat's turns while the conversation opened, ``stale``, is from
        another runtime than ``runtime``, the one its role runs on."""
        return Refused(INVALID, runtime_changed_message(self._name, stale.runtime, runtime))

    async def _answer(self, planner: PlannerChat, turn: WaitingTurn, parent: Span) -> TurnEndReason:
        """``planner``'s ``turn``, in its span, a child of ``parent``, or, for a root turn,
        starting its own trace linked to each of its links, saving the session after it, as its
        conversation may have changed."""
        # The span starts as the turn does, not as it's asked for, so a turn refused, or
        # cancelled before it runs, leaves no span unended.
        with self._hooks.tracing.turn(
            parent, planner.model(turn.settings), turn.links, root=turn.root
        ):
            reason = await planner.answer(turn.text, turn.settings)
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
        role's, re-reading its role, and remove its copy, if it has one, then start the turns
        waiting; refused while it
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
            self.advance()  # Messages delivered while the conversation was stale.

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
