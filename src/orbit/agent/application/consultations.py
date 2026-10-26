"""Consultations: a conversational role asking another, through ``consult``, and hearing its reply
later, as a message.

A consultation waits in the consulted role's line, in order with the user's own messages to it,
and runs as one of its turns, in its own trace linked to the asking turn. That turn's last message
is the reply, delivered to the asking role as a message of its own turn, with any others waiting.
A consultation that can't produce a reply is answered, the same way, saying why it was
interrupted. Those pending are saved with the session, and answered as it opens again. A role
asking past its hourly limit has its consultation held until the user decides on it.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.consult_approvals import Approvals, Held
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks, WaitingTurn
from orbit.agent.application.ports import Reply
from orbit.agent.domain.consult_limits import ConsultCount
from orbit.agent.domain.consultations import (
    CANCELLED,
    DECLINED,
    EXPIRED,
    RESTARTED,
    SWITCHED,
    interrupted_message,
)
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SavedConsultation
from orbit.protocol import (
    UNKNOWN,
    ApprovalDecided,
    ApprovalDecision,
    ApprovalRequested,
    ConsultationAnswered,
    ConsultationAsked,
    DecideApproval,
    Event,
    TurnEndReason,
)
from orbit.tracing import Span, current_span

OUTCOME = "orbit.consultation.outcome"


def title_of(message: str) -> str:
    """The title of a consultation asking ``message``: its first line."""
    return message.strip().splitlines()[0].strip()


def label(role: str) -> str:
    """The role ``role`` as a reply names it, capitalized."""
    return role[:1].upper() + role[1:]


def asked_note(consultant: str, number: int) -> str:
    """What the asking agent is told as consultation ``number`` of ``consultant`` is asked."""
    return f"Asked the {consultant} (consultation {number}); its reply will arrive as a message."


def held_note(consultant: str, number: int) -> str:
    """What the asking agent is told as consultation ``number`` of ``consultant`` is held for the
    user's approval."""
    return (
        f"Asked the {consultant} (consultation {number}), past your hourly limit, so it waits for"
        " the user's approval; its reply, or why it was interrupted, will arrive as a message."
    )


def reply_message(consultant: str, title: str, number: int, reply: str) -> str:
    """The message delivering ``consultant``'s ``reply`` to consultation ``number``, ``title``."""
    return f"{label(consultant)}'s reply to '{title}' (consultation {number}):\n{reply}"


def refusal(asker: str, role: Role, consultant: str) -> str | None:
    """Why the role ``asker``, ``role``, can't consult ``consultant``; none if it can."""
    if consultant in role.consults:
        return None
    named = ", ".join(role.consults)
    return f"The {asker} can't consult {consultant!r}; it consults {named}."


@dataclass
class Consultation:
    """Consultation ``number``: the role ``asker`` asking ``consultant`` ``message``, from the
    turn whose span is ``cause``, asked at ``asked`` on the loop's clock; ``position`` is its
    place in line last published, and ``left`` why it's interrupted as its session, saved,
    opens again."""

    number: int
    asker: str
    consultant: str
    message: str
    cause: Span
    asked: float
    position: int = field(default=0)
    left: str = field(default=RESTARTED)

    @property
    def title(self) -> str:
        return title_of(self.message)

    @property
    def saved(self) -> SavedConsultation:
        """The consultation as its session saves it, pending."""
        return SavedConsultation(
            number=self.number,
            asker=self.asker,
            consultant=self.consultant,
            title=self.title,
            why=self.left,
        )


class Consultations:
    """The session's consultations, numbered from 1: each waits in its consultant's seat among
    those ``seats`` finds, by role, publishing through ``post``, traced by the ``hooks``' tracing,
    saving the session with theirs as those pending change. While held, as turns stop, a consulted
    turn ending leaves its consultation pending, to answer as its session opens again. Each
    role's consultations are counted against its hourly limit, timed by the ``hooks``' timer;
    one past it is held for approval."""

    def __init__(
        self,
        seats: Callable[[str], PlannerSeat | None],
        roles: Callable[[], Mapping[str, Role]],
        post: Callable[[Event], None],
        hooks: SeatHooks,
    ) -> None:
        self._seats = seats
        self._roles = roles
        self._post = post
        self._tracing = hooks.tracing
        self._save = hooks.save
        self._count = 0
        self._pending: dict[int, Consultation] = {}  # Those asked and not yet answered.
        self._owed: list[SavedConsultation] = []  # Those the session opened was saved with.
        self._answering: dict[str, str] = {}  # The asker of each consultant's turn running.
        self._held = False
        self._clock = hooks.timer.clock
        self._counts: dict[str, ConsultCount] = {}  # Each asking role's, by role.
        self.approvals = Approvals[PlannerSeat](post, self._expired, hooks.timer)

    @property
    def saved(self) -> tuple[SavedConsultation, ...]:
        """The consultations pending, to save: those the session opened with still to answer,
        then those asked since."""
        return (*self._owed, *(asked.saved for asked in self._pending.values()))

    @property
    def last(self) -> int:
        """The number of the last consultation asked in the session, to save."""
        return self._count

    def asker(self, consultant: str) -> str | None:
        """The role whose consultation the role ``consultant``'s turn running answers; none
        while it answers none."""
        return self._answering.get(consultant)

    def hold(self) -> None:
        """Leave the consultations whose turns end pending, as turns stop, until
        :meth:`release`."""
        self._held = True

    def release(self) -> None:
        """Answer the consultations whose turns end again."""
        self._held = False

    async def cancel(self) -> None:
        """Answer each consultation pending, but those held for approval, as cancelled, its turn
        having been cancelled or forgotten as turns stopped, saving the session after."""
        held = {request.consultation for request in self.approvals.waiting}
        cancelled = [asked for asked in self._pending.values() if asked.number not in held]
        for asked in cancelled:
            self._drop(asked, CANCELLED)
        await self._save()

    def stop_expiry(self) -> None:
        """Stop timing the consultations held for approval, as the session's turns stop."""
        self.approvals.stop()

    def leave(self) -> None:
        """Have the consultations pending saved as interrupted by a switch of session."""
        for asked in self._pending.values():
            asked.left = SWITCHED

    def stay(self) -> None:
        """Have the consultations pending saved as interrupted by a restart again, as their
        session stays open."""
        for asked in self._pending.values():
            asked.left = RESTARTED

    def replace(self, saved: tuple[SavedConsultation, ...], last: int = 0) -> None:
        """Forget the consultations pending, saved with the session left, and hold ``saved``,
        those of the session opened, to answer by :meth:`settle`, numbering the next after
        ``last``."""
        self._pending, self._owed = {}, list(saved)
        self._count = last
        self.approvals.clear()

    async def settle(self) -> None:
        """Answer each consultation the session opened was saved with, as interrupted, saving
        the session once they're answered."""
        owed, self._owed = self._owed, []
        for saved in owed:
            self._interrupted(saved, saved.why)
        await self._save()

    async def ask(self, asker: str, consultant: str, message: str, cause: Span) -> Reply:
        """Have the role ``asker`` ask ``consultant`` ``message``, from the turn whose span is
        ``cause``; returns what the asking agent is told at once, refused for a role it doesn't
        consult, one without a seat, or an empty message."""
        seat = self._consultant(asker, consultant, message)
        if isinstance(seat, str):
            return Reply(seat, is_error=True)
        self._count += 1
        now = self._clock()
        asked = Consultation(self._count, asker, consultant, message, cause, now)
        self._pending[asked.number] = asked
        count = self._counts.get(asker, ConsultCount())
        if count.needs_approval(self._roles()[asker].consult_limit_per_hour, now):
            self.approvals.hold(_request(asked, count.recent(now)), seat)
            await self._save()
            return Reply(held_note(consultant, asked.number))
        self._start(asked, seat)
        await self._save()
        return Reply(asked_note(consultant, asked.number))

    def _start(self, asked: Consultation, seat: PlannerSeat) -> None:
        """Count ``asked`` against its asker's limit and have it wait in ``seat``'s line."""
        count = self._counts.get(asked.asker, ConsultCount())
        self._counts[asked.asker] = count.counted(self._clock())
        seat.wait(self._turn(asked, seat))

    async def decide(self, command: DecideApproval) -> None:
        """Settle the consultation held that ``command`` decides on: start it, freeing its asker
        of the limit for an hour if so decided, or answer it as declined; publish the decision.
        Refused for one that isn't held."""
        held = self.approvals.take(command.consultation)
        if held is None:
            message = f"No consultation {command.consultation} is waiting for approval."
            raise Refused(UNKNOWN, message)
        asked = self._pending[command.consultation]
        if command.decision == "allow_hour":
            count = self._counts.get(asked.asker, ConsultCount())
            self._counts[asked.asker] = count.freed(self._clock())
        self._post(_decided(asked.number, command.decision))
        if command.decision == "deny":
            self._drop(asked, DECLINED)
        else:
            self._start(asked, held.work)
        if command.decision == "allow_hour":
            self._free(asked.asker)
        await self._save()

    def _free(self, asker: str) -> None:
        """Start each other consultation held from the role ``asker``, now free of its limit,
        publishing that each was allowed for the hour."""
        for held in self.approvals.take_from(asker):
            self._post(_decided(held.request.consultation, "allow_hour"))
            self._start(self._pending[held.request.consultation], held.work)

    async def _expired(self, held: Held[PlannerSeat]) -> None:
        """Answer the consultation ``held`` as expired, denied, and publish that."""
        self._post(_decided(held.request.consultation, "deny"))
        self._drop(self._pending[held.request.consultation], EXPIRED)
        await self._save()

    def _drop(self, asked: Consultation, why: str) -> None:
        """Answer ``asked``, whose turn isn't running, as interrupted for ``why``."""
        del self._pending[asked.number]
        self._interrupted(asked.saved, why)

    def _consultant(self, asker: str, consultant: str, message: str) -> PlannerSeat | str:
        """The seat of ``consultant``, which ``asker`` asks ``message``; or why it can't."""
        problem = refusal(asker, self._roles()[asker], consultant)
        if problem is not None:
            return problem
        if not message.strip():
            return "A consultation needs a message."
        seat = self._seats(consultant)
        if seat is None or seat.disabled:
            return f"The {consultant} can't be consulted now."
        return seat.moved or seat

    def _turn(self, asked: Consultation, seat: PlannerSeat) -> WaitingTurn:
        """The consultant's turn answering ``asked``, in ``seat``'s line."""

        async def answered(reason: TurnEndReason, reply: str, why: str | None) -> None:
            await self._answer(asked, reason, reply, why)

        return WaitingTurn(
            asked.message,
            seat.settings,
            lambda: self._opened(asked),
            answered,
            lambda position: self._place(asked, position),
        )

    @contextmanager
    def _opened(self, asked: Consultation) -> Iterator[Span]:
        """The span of the turn answering ``asked``, current for the block, while which
        :meth:`asker` names its asker."""
        waited = self._clock() - asked.asked
        cause, asker, consultant = asked.cause, asked.asker, asked.consultant
        with self._tracing.consultation(cause, asker, consultant, round(waited * 1000)) as span:
            self._answering[consultant] = asker
            try:
                yield span
            finally:
                del self._answering[consultant]

    def _place(self, asked: Consultation, position: int) -> None:
        """Publish ``asked``'s ``position`` in line, unless it was published already."""
        if position == asked.position:
            return
        asked.position = position
        self._post(
            ConsultationAsked(
                consultation=asked.number,
                asker=asked.asker,
                consultant=asked.consultant,
                title=asked.title,
                position=position,
            )
        )

    async def _answer(
        self, asked: Consultation, reason: TurnEndReason, reply: str, why: str | None
    ) -> None:
        """Publish the ``reply`` to ``asked``, its turn having ended for ``reason``, recording
        that on the turn's span, and deliver it to the asker; or, if the turn gave none, say
        ``why`` it was interrupted. While held, ``asked`` stays pending."""
        if self._held:
            return
        del self._pending[asked.number]
        current_span().set_attributes({OUTCOME: reason})
        if why is None:
            self._replied(asked.saved, reply)
        else:
            self._interrupted(asked.saved, why)
        await self._save()

    def _replied(self, asked: SavedConsultation, reply: str) -> None:
        """Publish the consultant's ``reply`` to ``asked`` and deliver it to its asker."""
        self._post(_answered(asked, reply))
        message = reply_message(asked.consultant, asked.title, asked.number, reply)
        self._deliver(asked.asker, message)

    def _interrupted(self, asked: SavedConsultation, why: str) -> None:
        """Publish that ``asked`` was interrupted for ``why``, and tell its asker so."""
        self._post(_answered(asked, "", why))
        self._deliver(asked.asker, interrupted_message(asked.consultant, asked.title, why))

    def _deliver(self, asker: str, message: str) -> None:
        """Deliver ``message`` to the role ``asker``, if it still has a seat, linked to the
        consultation's span, the current one."""
        seat = self._seats(asker)
        if seat is not None:
            seat.deliver(message, current_span())


def _answered(asked: SavedConsultation, reply: str, why: str | None = None) -> ConsultationAnswered:
    """The event answering ``asked`` with ``reply``, or saying ``why`` it was interrupted."""
    return ConsultationAnswered(
        consultation=asked.number,
        asker=asked.asker,
        consultant=asked.consultant,
        title=asked.title,
        reply=reply,
        interrupted=why,
    )


def _request(asked: Consultation, recent: int) -> ApprovalRequested:
    """What the user is asked about ``asked``, its asker having asked ``recent`` this hour."""
    return ApprovalRequested(
        consultation=asked.number,
        asker=asked.asker,
        consultant=asked.consultant,
        title=asked.title,
        asked=recent,
    )


def _decided(consultation: int, decision: ApprovalDecision) -> ApprovalDecided:
    """The event saying consultation ``consultation``, held, was settled with ``decision``."""
    return ApprovalDecided(consultation=consultation, decision=decision)
