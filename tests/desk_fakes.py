"""A backend for testing what the TUI's models send, through a real CommandDesk."""

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
    answers a test gives; what the desk shows elsewhere is ``shown``. Sending raises
    ``send_error`` while one is set."""

    def __init__(self) -> None:
        self.sent: list[CommandEnvelope] = []
        self.shown: list[Shown | Event] = []
        self.send_error: Exception | None = None
        self.desk = CommandDesk(self._send, self.shown.append, self.shown.append)

    async def _send(self, envelope: CommandEnvelope) -> None:
        self.sent.append(envelope)
        if self.send_error is not None:
            raise self.send_error

    @property
    def commands(self) -> list[Command]:
        return [envelope.payload for envelope in self.sent]

    def accept(self, index: int = -1) -> None:
        """Accept the command sent at ``index``, the last by default."""
        self.desk.answer(CommandAccepted(command_id=self.sent[index].command_id))

    def refuse(self, message: str, code: str = INVALID, index: int = -1) -> CommandRefused:
        """Refuse the command sent at ``index``, the last by default, with ``code`` saying
        ``message``; the refusal."""
        refusal = CommandRefused(command_id=self.sent[index].command_id, code=code, message=message)
        self.desk.answer(refusal)
        return refusal
