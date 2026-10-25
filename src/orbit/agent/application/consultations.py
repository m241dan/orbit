"""Consultations: a conversational role asking another, through ``consult``, and hearing its reply
later, as a message.

A consultation waits in the consulted role's line, in order with the user's own messages to it,
and runs as one of its turns, in its own trace linked to the asking turn. That turn's last message
is the reply, delivered to the asking role as a message of its own turn, with any others waiting.
A consultation that can't produce a reply is answered, the same way, saying why it was
interrupted. Those pending are saved with the session, and answered as it opens again.
"""

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks, WaitingTurn
from orbit.agent.application.ports import Reply
from orbit.agent.domain.consultations import RESTARTED, SWITCHED, interrupted_message
from orbit.agent.domain.roles import Role
from orbit.agent.domain.sessions import SavedConsultation
from orbit.protocol import ConsultationAnswered, ConsultationAsked, Event, TurnEndReason
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
    turn ending leaves its consultation pending, to answer as its session opens again."""

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
        self._held = False

    @property
    def saved(self) -> tuple[SavedConsultation, ...]:
        """The consultations pending, to save: those the session opened with still to answer,
        then those asked since."""
        return (*self._owed, *(asked.saved for asked in self._pending.values()))

    def hold(self) -> None:
        """Leave the consultations whose turns end pending, as turns stop, until
        :meth:`release`."""
        self._held = True

    def release(self) -> None:
        """Answer the consultations whose turns end again."""
        self._held = False

    def leave(self) -> None:
        """Have the consultations pending saved as interrupted by a switch of session."""
        for asked in self._pending.values():
            asked.left = SWITCHED

    def replace(self, saved: tuple[SavedConsultation, ...]) -> None:
        """Forget the consultations pending, saved with the session left, and hold ``saved``,
        those of the session opened, to answer by :meth:`settle`."""
        self._pending, self._owed = {}, list(saved)

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
        loop = asyncio.get_running_loop()
        asked = Consultation(self._count, asker, consultant, message, cause, loop.time())
        self._pending[asked.number] = asked
        seat.wait(self._turn(asked, seat))
        await self._save()
        return Reply(asked_note(consultant, asked.number))

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
        return seat

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
        """The span of the turn answering ``asked``, current for the block."""
        waited = asyncio.get_running_loop().time() - asked.asked
        cause, asker, consultant = asked.cause, asked.asker, asked.consultant
        with self._tracing.consultation(cause, asker, consultant, round(waited * 1000)) as span:
            yield span

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
        """Deliver ``message`` to the role ``asker``, if it still has a seat."""
        seat = self._seats(asker)
        if seat is not None:
            seat.deliver(message)


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
