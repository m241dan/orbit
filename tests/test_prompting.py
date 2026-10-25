"""Talking to the planner, without Textual: what a prompt sends and shows, how the prompt is
marked busy, and what refusals and failures to send do to the prompt and its draft."""

from collections.abc import AsyncIterator

from orbit.protocol import (
    NOT_ACTIVE,
    PLANNER,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    CommandAccepted,
    Event,
    MessageSent,
    Notice,
    RunTasks,
    SendMessage,
    TurnFinished,
    TurnStarted,
)
from tests.frontend_fakes import Shell

BUSY = "Prompt · planner · busy"
FREE = "Prompt · planner"
HI = SendMessage(agent=PLANNER, text="hi")
GONE = "Couldn't send the command: the backend is gone"


async def test_a_prompt_is_sent_and_the_planners_turn_shows_under_it() -> None:
    shell = Shell()
    await shell.submit("hi")
    assert shell.wire.commands == [HI]
    # Busy from now, not from the backend's notice, so a second prompt can't race it.
    assert shell.marks.title == BUSY
    assert not shell.prompt.typing
    await shell.feed(
        BusyChanged(busy=True, lane="planner"),
        TurnStarted(turn_id="t"),
        AssistantMessage(turn_id="t", text="hello back"),
        TurnFinished(turn_id="t", reason="completed"),
    )
    assert shell.shown() == ["hi", "hello back", "done · 0s"]
    assert shell.marks.title == BUSY
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    assert shell.marks.title == FREE


async def test_a_prompt_shows_once_when_the_backend_echoes_it() -> None:
    shell = Shell()
    await shell.submit("hi")
    sent = shell.wire.sent[-1].command_id
    await shell.feed(
        MessageSent(command_id=sent, agent=PLANNER, text="hi"), CommandAccepted(command_id=sent)
    )
    assert shell.entries() == [("hi", "prompt")]
    assert shell.tab_entries(PLANNER) == ["hi"]


async def test_another_frontends_message_shows_as_a_prompt_to_the_planner() -> None:
    shell = Shell()
    await shell.feed(MessageSent(command_id="other", agent=PLANNER, text="yo"))
    assert shell.entries() == [("yo", "prompt")]
    assert shell.tab_entries(PLANNER) == ["yo"]


async def test_each_command_goes_in_an_envelope_with_a_fresh_id() -> None:
    shell = Shell()
    await shell.submit("hi")
    await shell.frontend.stop_planner()
    first, second = shell.wire.sent
    assert (first.payload, second.payload) == (HI, CancelTurn(lane="planner"))
    for envelope in (first, second):
        assert len(envelope.command_id) == 32
        int(envelope.command_id, 16)  # Hex.
        assert (envelope.workspace, envelope.session) == (None, None)
    assert first.command_id != second.command_id


async def test_a_refused_prompt_shows_why_and_frees_the_prompt_keeping_the_draft() -> None:
    shell = Shell()
    await shell.submit("hi")
    await shell.feed(CommandAccepted(command_id="other"))
    assert shell.marks.title == BUSY
    await shell.refuse("unknown", "Not now.")
    assert shell.entries()[-1] == ("Not now.", "error")
    assert shell.marks.title == FREE
    assert shell.prompt.text == "hi"  # The draft is back, to send again.
    await shell.frontend.submit(shell.prompt.text)
    assert shell.wire.commands == [HI, HI]


async def test_a_prompt_refused_as_not_active_is_withdrawn_and_its_draft_kept() -> None:
    shell = Shell()
    await shell.submit("hi")
    await shell.refuse(NOT_ACTIVE, "Not here.")
    assert shell.entries() == [("Not here.", "error")]
    assert shell.tab_entries(PLANNER) == []
    assert (shell.prompt.text, shell.marks.title) == ("hi", FREE)


async def test_a_refusal_keeps_the_draft_as_edited_since_sending() -> None:
    shell = Shell()
    await shell.submit("hi")
    shell.prompt.text = "hi more"
    await shell.refuse(NOT_ACTIVE, "Not here.")
    assert shell.entries() == [("Not here.", "error")]
    assert shell.prompt.text == "hi more"


async def test_a_prompt_sent_keeps_its_text_in_the_prompt_until_accepted() -> None:
    shell = Shell()
    await shell.submit("hi")
    assert (shell.prompt.text, shell.marks.title) == ("hi", BUSY)
    await shell.accept_last()
    assert shell.entries() == [("hi", "prompt")]
    assert (shell.prompt.text, shell.marks.title) == ("", BUSY)


async def test_an_accepted_prompt_keeps_a_draft_edited_since_sending() -> None:
    shell = Shell()
    await shell.submit("hi")
    shell.prompt.text = "hi more"
    await shell.accept_last()
    assert shell.entries() == [("hi", "prompt")]
    assert shell.prompt.text == "hi more"


async def test_a_refusal_of_an_earlier_prompt_leaves_the_planner_busy() -> None:
    shell = Shell()
    await shell.submit("hi")
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    await shell.submit("again")
    await shell.refuse("unknown", "Late.", index=0)
    assert shell.entries()[-1] == ("Late.", "error")
    assert shell.marks.title == BUSY


async def test_a_refusal_of_another_command_leaves_the_planner_busy() -> None:
    shell = Shell()
    await shell.submit("hi")
    await shell.frontend.stop_planner()
    await shell.refuse("unknown", "No.")
    assert shell.entries()[-1] == ("No.", "error")
    assert shell.marks.title == BUSY


async def test_a_failed_event_stream_shows_why_and_ends() -> None:
    shell = Shell()

    async def batches() -> AsyncIterator[list[Event]]:
        yield [Notice(message="before")]
        raise RuntimeError("the queue broke")

    reading = batches()
    shell.frontend.show_batch(await shell.frontend.next_batch(reading))
    assert await shell.frontend.next_batch(reading) == []
    assert shell.shown()[-1] == "The backend's event stream failed: the queue broke"


async def test_the_frontend_says_hello_and_shows_a_failure_to() -> None:
    shell = Shell()
    assert await shell.frontend.hello()
    shell.wire.hello_error = RuntimeError("no git")
    assert not await shell.frontend.hello()
    assert shell.shown()[-1] == "The backend failed to start the session: no git"


async def test_a_prompt_that_fails_to_send_shows_why_and_frees_the_prompt() -> None:
    shell = Shell()
    shell.wire.send_error = RuntimeError("the backend is gone")
    await shell.submit("hi")
    assert shell.entries()[-1] == (GONE, "error")
    assert shell.marks.title == FREE
    assert shell.prompt.text == "hi"  # The draft is back.
    await shell.frontend.submit(shell.prompt.text)
    assert shell.wire.commands == [HI, HI]


async def test_running_tasks_when_the_send_fails_shows_why() -> None:
    shell = Shell()
    shell.wire.send_error = RuntimeError("the backend is gone")
    await shell.frontend.run_tasks()
    assert shell.wire.commands == [RunTasks()]
    assert shell.entries()[-1] == (GONE, "error")


async def test_another_command_that_fails_to_send_leaves_the_planner_busy() -> None:
    shell = Shell()
    await shell.submit("hi")
    shell.wire.send_error = RuntimeError("the backend is gone")
    await shell.frontend.stop_planner()
    assert shell.entries()[-1] == (GONE, "error")
    assert shell.marks.title == BUSY


async def test_a_prompt_while_the_planner_is_busy_is_kept_and_sent_once_it_is_free() -> None:
    shell = Shell()
    await shell.feed(BusyChanged(busy=True, lane="planner"))
    await shell.submit("hi")
    assert shell.wire.commands == []
    assert shell.app.warnings == ["Planner is busy"]
    assert (shell.prompt.text, shell.prompt.typing) == ("hi", True)
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    assert shell.wire.commands == []
    await shell.frontend.submit(shell.prompt.text)  # Still typing, so enter alone sends it.
    assert shell.wire.commands == [HI]
    await shell.accept_last()
    assert shell.prompt.text == ""


async def test_a_second_prompt_before_the_first_is_answered_sends_nothing() -> None:
    shell = Shell()
    await shell.submit("a")
    await shell.submit("ab")
    assert shell.wire.commands == [SendMessage(agent=PLANNER, text="a")]
    assert shell.prompt.text == "ab"


async def test_stopping_the_run_sends_cancel_for_the_work_lane() -> None:
    shell = Shell()
    await shell.frontend.cancel_turn()
    assert shell.wire.commands == [CancelTurn()]


async def test_escape_with_the_prompt_selected_sends_nothing() -> None:
    shell = Shell()
    shell.frontend.release_cursor()
    assert shell.wire.commands == []


async def test_a_blank_prompt_is_ignored() -> None:
    shell = Shell()
    await shell.submit(" ")
    assert shell.wire.commands == []
    assert shell.shown() == []
