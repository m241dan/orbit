"""A backend that a model's commands reach through a real CommandDesk, answered as a test says."""

from orbit.protocol import (
    INVALID,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
)
from orbit.tui.commands import CommandDesk, Shown


class Backend:
    """A backend a model sends its commands to through a real :class:`CommandDesk`, whose
    answers a test gives; what the desk shows elsewhere is ``shown``, and while ``failure`` is
    set, sending raises it."""

    def __init__(self) -> None:
        self.sent: list[CommandEnvelope] = []
        self.shown: list[Shown | Event] = []
        self.failure: Exception | None = None
        self.desk = CommandDesk(self._send, self.shown.append, self.shown.append)

    async def _send(self, envelope: CommandEnvelope) -> None:
        self.sent.append(envelope)
        if self.failure is not None:
            raise self.failure

    @property
    def commands(self) -> list[Command]:
        return [envelope.payload for envelope in self.sent]

    def accept(self, index: int = -1) -> None:
        """Accept the command sent at ``index``, the last by default."""
        self.desk.answer(CommandAccepted(command_id=self.sent[index].command_id))

    def refuse(self, message: str, index: int = -1) -> None:
        """Refuse the command sent at ``index``, the last by default, as ``invalid``."""
        command_id = self.sent[index].command_id
        self.desk.answer(CommandRefused(command_id=command_id, code=INVALID, message=message))
