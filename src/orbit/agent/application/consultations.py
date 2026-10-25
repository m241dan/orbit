"""Consultations: a conversational role asking another, through ``consult``, and hearing its reply
later, as a message.

A consultation waits in the consulted role's line, in order with the user's own messages to it,
and runs as one of its turns, in its own trace linked to the asking turn. That turn's last message
is the reply, delivered to the asking role as a message of its own turn, with any others waiting.
"""

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from orbit.agent.application.planner_seat import PlannerSeat, WaitingTurn
from orbit.agent.application.planner_trace import PlannerTracing
from orbit.agent.application.ports import Reply
from orbit.agent.domain.roles import Role
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
    place in line last published."""

    number: int
    asker: str
    consultant: str
    message: str
    cause: Span
    asked: float
    position: int = field(default=0)

    @property
    def title(self) -> str:
        return title_of(self.message)


class Consultations:
    """The session's consultations, numbered from 1: each waits in its consultant's seat among
    those ``seats`` finds, by role, publishing through ``post``, traced by ``tracing``."""

    def __init__(
        self,
        seats: Callable[[str], PlannerSeat | None],
        roles: Callable[[], Mapping[str, Role]],
        post: Callable[[Event], None],
        tracing: PlannerTracing,
    ) -> None:
        self._seats = seats
        self._roles = roles
        self._post = post
        self._tracing = tracing
        self._count = 0

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
        seat.wait(self._turn(asked, seat))
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

        async def answered(reason: TurnEndReason, reply: str) -> None:
            await self._answer(asked, reason, reply)

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

    async def _answer(self, asked: Consultation, reason: TurnEndReason, reply: str) -> None:
        """Publish the ``reply`` to ``asked``, its turn having ended for ``reason``, recording
        that on the turn's span, and deliver it to the asker."""
        current_span().set_attributes({OUTCOME: reason})
        self._post(
            ConsultationAnswered(
                consultation=asked.number,
                asker=asked.asker,
                consultant=asked.consultant,
                title=asked.title,
                reply=reply,
            )
        )
        seat = self._seats(asked.asker)
        assert seat is not None  # It asked from its seat.
        seat.deliver(reply_message(asked.consultant, asked.title, asked.number, reply))
