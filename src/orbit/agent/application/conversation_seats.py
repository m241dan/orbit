"""The seats of a session's conversational roles: one for each enabled role you talk to, each with
its own conversation, its turns in its role's lane."""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from functools import partial

from orbit.agent.application.closing import close_all
from orbit.agent.application.consultations import Consultations
from orbit.agent.application.desk import AgentDesk
from orbit.agent.application.outbox import EventSink
from orbit.agent.application.planner_seat import (
    PlannerSeat,
    SeatHooks,
    SeatLanes,
    SeatParts,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.refusals import Refused, unknown
from orbit.agent.domain.roles import Role, conversational_roles, disabled_note
from orbit.agent.domain.sessions import SavedConsultation, SavedConversation, SavedCopy
from orbit.protocol import (
    NOT_CONFIGURED,
    PLANNER,
    ApprovalRequested,
    BusyLane,
    DecideApproval,
)


class ConversationSeats:
    """The conversational roles' seats in a session with ``desk``'s agents, proposing into
    ``tasks`` and publishing into ``events``, each asking ``hooks`` of the session: the planner's
    first, there even without agents or a planner role, or while it's disabled, as it refuses
    their work then, and then one for each other enabled role with ``conversation = true``, in
    the roles' order. A disabled planner's lane is the roles' only while another role answers
    there. The seats in a lane take turns: one answers there at a time. The conversations of
    roles without a seat that a session was opened with are kept, to save back, as are those
    of roles that now run on another runtime than their conversation's, until they're reset.
    The roles working in a copy take their turns in theirs among the ``hooks``' copies. The roles
    consult those their role names, each consultation waiting in its consultant's line; a lane
    freed starts the next turn waiting for a seat in it."""

    def __init__(
        self, desk: AgentDesk | None, tasks: Tasks, events: EventSink, hooks: SeatHooks
    ) -> None:
        self._desk = desk
        self._copies = hooks.copies
        roles = desk.agents.roles if desk else {}
        names = dict.fromkeys((PLANNER, *conversational_roles(roles)))
        consultations = Consultations(self.get, self._roles, events.put_nowait, hooks)
        self._consultations = consultations
        lanes = SeatLanes(events.put_nowait, self._advance)
        parts = SeatParts(desk, tasks, events, hooks, lanes, consultations.ask)
        self._seats = {name: PlannerSeat(parts, name) for name in names}
        # The conversations opened of roles without a seat.
        self._kept: dict[str, SavedConversation] = {}

    def _roles(self) -> Mapping[str, Role]:
        """The session's roles, as they are now, which only its agents, consulting, ask for."""
        assert self._desk is not None
        return self._desk.agents.roles

    def _advance(self, lane: BusyLane) -> None:
        """Start the next turn waiting for a seat in ``lane``, now that it's free."""
        for seat in self._in(lane):
            seat.advance()

    @property
    def names(self) -> tuple[str, ...]:
        """The roles with a seat, which the user can talk to: the planner, then the others."""
        return tuple(self._seats)

    @property
    def planner(self) -> PlannerSeat:
        """The planner's seat."""
        return self._seats[PLANNER]

    def get(self, role: str) -> PlannerSeat | None:
        """Role ``role``'s seat; none if it has none."""
        return self._seats.get(role)

    def seat(self, role: str, problem: str, listing: str) -> PlannerSeat:
        """Role ``role``'s seat. Refused, coded ``not_configured``, for a conversational role
        that's disabled, and otherwise, coded ``unknown``, for a role without a seat, with the
        ``problem`` with it and the ``listing`` naming those with one."""
        seat = self._seats.get(role)
        if seat is not None:
            return seat
        found = self._desk.agents.roles.get(role) if self._desk else None
        if found is not None and found.conversation:  # Only a disabled one has no seat.
            raise Refused(NOT_CONFIGURED, disabled_note(role))
        raise unknown(problem, listing, self.names)

    @property
    def answering(self) -> tuple[str, ...]:
        """The roles answering: the planner, if it is, then the others, in order."""
        return tuple(name for name, seat in self._seats.items() if seat.running)

    @property
    def lanes(self) -> tuple[BusyLane, ...]:
        """The lanes the roles answer in, the planner's first, each named once: a disabled
        planner's only while another role answers there."""
        return tuple(dict.fromkeys(seat.lane for seat in self._open()))

    def busy(self) -> dict[BusyLane, bool]:
        """Whether each of the roles' lanes is busy, by name: a role in it is answering."""
        return {lane: any(seat.running for seat in self._in(lane)) for lane in self.lanes}

    def cancel(self, lane: BusyLane) -> None:
        """Cancel the turns of the roles in ``lane``, if they run."""
        for seat in self._in(lane):
            seat.cancel()

    def _open(self) -> Iterator[PlannerSeat]:
        """The seats whose roles aren't disabled: every one but a disabled planner's."""
        return (seat for seat in self._seats.values() if not seat.disabled)

    def _in(self, lane: BusyLane) -> Iterator[PlannerSeat]:
        """The seats whose turns run in ``lane``."""
        return (seat for seat in self._seats.values() if seat.lane == lane)

    @property
    def conversations(self) -> dict[str, SavedConversation]:
        """Each role's conversation, by role, with the runtime it ran on, to save: those of the
        roles with a seat that have one, then those kept of roles without a seat."""
        held = {name: seat.conversation for name, seat in self._seats.items()}
        return {name: c for name, c in held.items() if c is not None} | self._kept

    @property
    def copies(self) -> dict[str, SavedCopy]:
        """The copies the roles work in, by role, to save; none without copies."""
        return self._copies.held if self._copies else {}

    @property
    def consultations(self) -> tuple[SavedConsultation, ...]:
        """The consultations pending, to save."""
        return self._consultations.saved

    def asker(self, role: str) -> str | None:
        """The role whose consultation the role ``role``'s turn running answers; none while it
        answers none."""
        return self._consultations.asker(role)

    @property
    def approvals(self) -> tuple[ApprovalRequested, ...]:
        """What the user is asked about the consultations held for approval."""
        return self._consultations.approvals.waiting

    def viewers(self, count: int) -> None:
        """Note that ``count`` frontends view the session, as held consultations expire only
        with none."""
        self._consultations.approvals.viewers(count)

    async def decide(self, command: DecideApproval) -> None:
        """Settle the consultation held that ``command`` decides on; refused for one not held."""
        await self._consultations.decide(command)

    def leave(self) -> None:
        """Have the consultations pending saved as interrupted by a switch of session."""
        self._consultations.leave()

    async def settle(self) -> None:
        """Answer each consultation the session opened was saved with, as interrupted."""
        await self._consultations.settle()

    @property
    def notes(self) -> tuple[str, ...]:
        """What the planner is still to hear, to save."""
        return self.planner.notes

    def tell(self, note: str) -> None:
        """Have the planner, if there is one, hear ``note`` ahead of its next prompt."""
        self.planner.tell(note)

    def replace(
        self,
        conversations: Mapping[str, SavedConversation],
        notes: tuple[str, ...] = (),
        copies: Mapping[str, SavedCopy] | None = None,
        consultations: tuple[SavedConsultation, ...] = (),
    ) -> None:
        """Have each role carry on its conversation among ``conversations``, if it has one, the
        planner still to hear ``notes``, keeping those of roles without a seat, and work in its
        copy among ``copies``; the old ones are closed by :meth:`close_retired`, once the switch
        is done, and the old copies kept where they are. The turns waiting are forgotten, and the
        consultations pending with them, saved with the session left; ``consultations``, those
        of the session opened, are answered by :meth:`settle`."""
        self._hold_copies(copies or {})
        self._consultations.replace(consultations)
        for name, seat in self._seats.items():
            seat.drop_waiting()
            seat.replace(conversations.get(name), notes if name == PLANNER else ())
        self._kept = {name: c for name, c in conversations.items() if name not in self._seats}

    def _hold_copies(self, copies: Mapping[str, SavedCopy]) -> None:
        """Have the roles work in ``copies``, if they work in any."""
        if self._copies is not None:
            self._copies.replace(copies)

    async def close_retired(self) -> None:
        """Close each role's retired conversations, every role's even if one fails, as
        :func:`close_all` does."""
        await close_all(self._each(PlannerSeat.close_retired))

    async def stop_turns(self) -> None:
        """Cancel each role's turn, if it runs, and wait for them to end, starting none of the
        turns waiting meanwhile, which are forgotten; the consultations they answered stay
        pending, to answer as the session opens again."""
        self._consultations.hold()
        self._consultations.stop_expiry()
        for seat in self._seats.values():
            seat.hold()
        try:
            await close_all(self._each(PlannerSeat.stop_turn))
        finally:
            for seat in self._seats.values():
                seat.drop_waiting()
            self._consultations.release()

    async def close(self) -> None:
        """End each role's current conversation, at the session's end."""
        await close_all(self._each(PlannerSeat.close))

    def _each(
        self, stop: Callable[[PlannerSeat], Awaitable[None]]
    ) -> list[Callable[[], Awaitable[None]]]:
        """``stop`` of each seat, in order, for :func:`close_all`."""
        return [partial(stop, seat) for seat in self._seats.values()]
