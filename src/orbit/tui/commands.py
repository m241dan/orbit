"""Sending commands to the backend and following what it makes of each."""

from collections.abc import Awaitable, Callable
from uuid import uuid4

from orbit.protocol import (
    INTERNAL,
    INVALID,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    DecideProposal,
    ErrorOccurred,
    Event,
    MessageSent,
    NotConnected,
    ProposalDecided,
    SendMessage,
    Welcome,
)
from orbit.tui.view.outcomes import Awaited, Outcomes, awaiting, settled

Send = Callable[[CommandEnvelope], Awaitable[None]]
"""Sends a command to the open session."""
Answer = CommandAccepted | CommandRefused | Welcome | MessageSent | ProposalDecided
"""What the backend makes of a command: its outcome, or the message or decision it accepted."""
Shown = CommandRefused | MessageSent | ProposalDecided
"""The answers the conversation shows."""


class CommandDesk:
    """Sends commands with ``send`` under fresh ids, doing what awaits the latest command of
    each kind once it's accepted or refused; ``report`` takes an event standing in for the
    backend's when a command isn't sent, and ``show`` an answer the conversation shows."""

    def __init__(
        self, send: Send, report: Callable[[Event], None], show: Callable[[Shown], None]
    ) -> None:
        self._send = send
        self._report = report
        self._show = show
        self._outcomes = Outcomes()  # What awaits the outcomes of the commands sent.
        # The command ids of the messages and decisions sent, shown already, whose echo isn't.
        self._echoes: set[str] = set()

    async def send(
        self,
        command: Command,
        awaited: Awaited | None = None,
        failure: str = "Couldn't send the command",
    ) -> bool:
        """Whether ``command`` was sent, under a fresh id remembered as :meth:`_remember` does,
        with ``awaited`` awaiting its outcome; a failure to send it is reported as
        :meth:`_unsent` does."""
        command_id = uuid4().hex
        self._remember(command, command_id)
        if awaited is not None:
            self._outcomes = awaiting(self._outcomes, command_id, command.type, awaited)
        try:
            await self._send(CommandEnvelope(command_id=command_id, payload=command))
        except Exception as exc:
            self._unsent(command_id, exc, failure)
            return False
        return True

    def answer(self, event: Answer) -> None:
        """Follow what the backend made of a command: its outcome, or the message or decision
        it accepted."""
        if isinstance(event, MessageSent | ProposalDecided):
            self._echo(event)
        else:
            self._outcome(event)

    def _remember(self, command: Command, command_id: str) -> None:
        """Remember ``command_id`` for a message or decision, whose echo isn't shown."""
        if isinstance(command, SendMessage | DecideProposal):
            self._echoes.add(command_id)

    def _unsent(self, command_id: str, exc: Exception, failure: str) -> None:
        """Report that the command ``command_id`` wasn't sent for ``exc``: refused while not
        connected, else failed, after ``failure``; either does what awaits its refusal."""
        if isinstance(exc, NotConnected):
            self._report(CommandRefused(command_id=command_id, code=INVALID, message=str(exc)))
            return
        self._echoes.discard(command_id)
        self._settle(command_id, accepted=False)
        self._report(ErrorOccurred(code=INTERNAL, message=f"{failure}: {exc}"))

    def _echo(self, event: MessageSent | ProposalDecided) -> None:
        """Show a message or decision a frontend sent, unless it's one this one sent and showed
        when sending it."""
        if event.command_id in self._echoes:
            self._echoes.discard(event.command_id)
        else:
            self._show(event)

    def _outcome(self, event: CommandAccepted | CommandRefused | Welcome) -> None:
        """Show why a command was refused, doing what awaits its outcome; an accepted one, or
        the Welcome answering Hello, shows nothing."""
        self._echoes.discard(event.command_id)
        if isinstance(event, Welcome):
            return
        self._settle(event.command_id, accepted=isinstance(event, CommandAccepted))
        if isinstance(event, CommandRefused):
            self._show(event)

    def _settle(self, command_id: str, accepted: bool) -> None:
        """Do what awaits ``command_id`` being ``accepted``, or else refused, if anything does."""
        self._outcomes, awaited = settled(self._outcomes, command_id)
        if awaited is None:
            return
        if accepted:
            awaited.accepted()
        else:
            awaited.refused()
