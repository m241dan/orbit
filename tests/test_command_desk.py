"""The CommandDesk sending commands and following what the backend makes of each."""

from orbit.protocol import (
    INTERNAL,
    INVALID,
    PLANNER,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ErrorOccurred,
    Event,
    Hello,
    MessageSent,
    NotConnected,
    RunTasks,
    SendMessage,
    Welcome,
)
from orbit.tui.commands import CommandDesk, Shown
from orbit.tui.view.outcomes import Awaited


class Desk:
    """A CommandDesk recording what it sends, reports and shows; sends raise ``failing``."""

    def __init__(self) -> None:
        self.sent: list[CommandEnvelope] = []
        self.reported: list[Event] = []
        self.shown: list[Shown] = []
        self.log: list[str] = []
        self.failing: Exception | None = None
        self.desk = CommandDesk(self._send, self.reported.append, self.shown.append)

    async def _send(self, envelope: CommandEnvelope) -> None:
        if self.failing is not None:
            raise self.failing
        self.sent.append(envelope)

    def noted(self, text: str) -> Awaited:
        """Awaiting an outcome by logging ``text`` and how it went."""
        return Awaited(
            accepted=lambda: self.log.append(f"{text} accepted"),
            refused=lambda: self.log.append(f"{text} refused"),
        )

    @property
    def last_id(self) -> str:
        return self.sent[-1].command_id


MESSAGE = SendMessage(agent=PLANNER, text="hi")


async def test_a_command_sent_goes_out_under_a_fresh_id() -> None:
    desk = Desk()
    assert await desk.desk.send(RunTasks())
    assert await desk.desk.send(RunTasks())
    assert [envelope.payload for envelope in desk.sent] == [RunTasks(), RunTasks()]
    assert desk.sent[0].command_id != desk.sent[1].command_id


async def test_the_echo_of_a_message_sent_here_is_skipped() -> None:
    desk = Desk()
    await desk.desk.send(MESSAGE)
    desk.desk.answer(MessageSent(command_id=desk.last_id, agent=PLANNER, text="hi"))
    assert desk.shown == []


async def test_the_echo_of_a_message_from_another_frontend_is_shown() -> None:
    desk = Desk()
    await desk.desk.send(MESSAGE)
    desk.desk.answer(MessageSent(command_id=desk.last_id, agent=PLANNER, text="hi"))
    echo = MessageSent(command_id="elsewhere", agent=PLANNER, text="hi")
    desk.desk.answer(echo)
    assert desk.shown == [echo]


async def test_the_echo_of_a_message_whose_outcome_came_first_is_shown() -> None:
    desk = Desk()
    await desk.desk.send(MESSAGE)
    desk.desk.answer(CommandAccepted(command_id=desk.last_id))
    echo = MessageSent(command_id=desk.last_id, agent=PLANNER, text="hi")
    desk.desk.answer(echo)
    assert desk.shown == [echo]


async def test_an_accepted_command_does_what_awaits_its_acceptance_and_shows_nothing() -> None:
    desk = Desk()
    await desk.desk.send(RunTasks(), desk.noted("run"))
    desk.desk.answer(CommandAccepted(command_id=desk.last_id))
    desk.desk.answer(CommandAccepted(command_id=desk.last_id))
    assert desk.log == ["run accepted"]
    assert desk.shown == []


async def test_a_refused_command_does_what_awaits_its_refusal_and_is_shown() -> None:
    desk = Desk()
    await desk.desk.send(RunTasks(), desk.noted("run"))
    refusal = CommandRefused(command_id=desk.last_id, code=INVALID, message="no")
    desk.desk.answer(refusal)
    assert desk.log == ["run refused"]
    assert desk.shown == [refusal]


async def test_only_the_latest_command_of_each_kind_awaits_its_outcome() -> None:
    desk = Desk()
    await desk.desk.send(RunTasks(), desk.noted("first"))
    first = desk.last_id
    await desk.desk.send(RunTasks(), desk.noted("second"))
    second = desk.last_id
    desk.desk.answer(CommandAccepted(command_id=first))
    desk.desk.answer(CommandAccepted(command_id=second))
    assert desk.log == ["second accepted"]


async def test_a_welcome_settles_nothing() -> None:
    desk = Desk()
    await desk.desk.send(RunTasks(), desk.noted("run"))
    desk.desk.answer(Welcome(command_id=desk.last_id, server_version="1"))
    assert desk.log == []
    assert desk.shown == []


async def test_a_command_sent_while_not_connected_is_reported_refused() -> None:
    desk = Desk()
    desk.failing = NotConnected()
    assert not await desk.desk.send(MESSAGE, desk.noted("message"))
    [refusal] = desk.reported
    assert isinstance(refusal, CommandRefused)
    assert (refusal.code, refusal.message) == (INVALID, str(NotConnected()))
    desk.desk.answer(refusal)
    assert desk.log == ["message refused"]
    assert desk.shown == [refusal]


async def test_a_command_that_fails_to_send_settles_refused_and_reports_the_failure() -> None:
    desk = Desk()
    desk.failing = RuntimeError("boom")
    hello = Hello(client="test", protocol_version=1)
    assert not await desk.desk.send(hello, desk.noted("hello"), failure="Couldn't start")
    assert desk.log == ["hello refused"]
    assert desk.reported == [ErrorOccurred(code=INTERNAL, message="Couldn't start: boom")]


async def test_a_failure_to_send_reports_the_default_failure_text() -> None:
    desk = Desk()
    desk.failing = RuntimeError("boom")
    assert not await desk.desk.send(RunTasks())
    assert desk.reported == [
        ErrorOccurred(code=INTERNAL, message="Couldn't send the command: boom")
    ]
