"""The seats of a session's conversational roles: one for each enabled role you talk to, each with
its own conversation, its turns in its role's lane."""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from functools import partial

from orbit.agent.application.closing import close_all
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
from orbit.agent.domain.roles import conversational_roles, disabled_note
from orbit.protocol import NOT_CONFIGURED, PLANNER, BusyLane


class ConversationSeats:
    """The conversational roles' seats in a session with ``desk``'s agents, proposing into
    ``tasks`` and publishing into ``events``, each asking ``hooks`` of the session: the planner's
    first, there even without agents or a planner role, or while it's disabled, as it refuses
    their work then, and then one for each other enabled role with ``conversation = true``, in
    the roles' order. The seats in a lane take turns: one answers there at a time. The
    conversations of roles without a seat that a session was opened with are kept, to save
    back."""

    def __init__(
        self, desk: AgentDesk | None, tasks: Tasks, events: EventSink, hooks: SeatHooks
    ) -> None:
        self._desk = desk
        roles = desk.agents.roles if desk else {}
        names = dict.fromkeys((PLANNER, *conversational_roles(roles)))
        parts = SeatParts(desk, tasks, events, hooks, SeatLanes(events.put_nowait))
        self._seats = {name: PlannerSeat(parts, name) for name in names}
        self._kept: dict[str, str] = {}  # The conversations opened of roles without a seat.

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
    def running(self) -> bool:
        """Whether any of the roles is answering."""
        return any(seat.running for seat in self._seats.values())

    @property
    def lanes(self) -> tuple[BusyLane, ...]:
        """The lanes the roles answer in, the planner's first, each named once."""
        return tuple(dict.fromkeys(seat.lane for seat in self._seats.values()))

    def busy(self) -> dict[BusyLane, bool]:
        """Whether each of the roles' lanes is busy, by name: a role in it is answering."""
        return {lane: any(seat.running for seat in self._in(lane)) for lane in self.lanes}

    def cancel(self, lane: BusyLane) -> None:
        """Cancel the turns of the roles in ``lane``, if they run."""
        for seat in self._in(lane):
            seat.cancel()

    def _in(self, lane: BusyLane) -> Iterator[PlannerSeat]:
        """The seats whose turns run in ``lane``."""
        return (seat for seat in self._seats.values() if seat.lane == lane)

    @property
    def conversations(self) -> dict[str, str]:
        """Each role's conversation, by role, to save: those of the roles with a seat that have
        one, then those kept of roles without a seat."""
        held = {name: seat.conversation_id for name, seat in self._seats.items()}
        return {name: c for name, c in held.items() if c is not None} | self._kept

    @property
    def notes(self) -> tuple[str, ...]:
        """What the planner is still to hear, to save."""
        return self.planner.notes

    def tell(self, note: str) -> None:
        """Have the planner, if there is one, hear ``note`` ahead of its next prompt."""
        self.planner.tell(note)

    def replace(self, conversations: Mapping[str, str], notes: tuple[str, ...] = ()) -> None:
        """Have each role carry on its conversation among ``conversations``, if it has one, the
        planner still to hear ``notes``, keeping those of roles without a seat; the old ones are
        closed by :meth:`close_retired`, once the switch is done."""
        for name, seat in self._seats.items():
            seat.replace(conversations.get(name), notes if name == PLANNER else ())
        self._kept = {name: c for name, c in conversations.items() if name not in self._seats}

    async def close_retired(self) -> None:
        """Close each role's retired conversations, every role's even if one fails, as
        :func:`close_all` does."""
        await close_all(self._each(PlannerSeat.close_retired))

    async def stop_turns(self) -> None:
        """Cancel each role's turn, if it runs, and wait for them to end."""
        await close_all(self._each(PlannerSeat.stop_turn))

    async def close(self) -> None:
        """End each role's current conversation, at the session's end."""
        await close_all(self._each(PlannerSeat.close))

    def _each(
        self, stop: Callable[[PlannerSeat], Awaitable[None]]
    ) -> list[Callable[[], Awaitable[None]]]:
        """``stop`` of each seat, in order, for :func:`close_all`."""
        return [partial(stop, seat) for seat in self._seats.values()]
