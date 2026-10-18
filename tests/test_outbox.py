"""The outbox the session publishes into, stamping each event as it's put in."""

import asyncio

from orbit.agent.application.outbox import Outbox
from orbit.protocol import EventEnvelope, Notice, SessionsListed, ToolCallFinished, Welcome
from tests.agent_fakes import FakeStore

ONE = Notice(message="one")
TWO = Notice(message="two")


class Open:
    """The session an outbox reads as open, changed by setting ``id``."""

    def __init__(self) -> None:
        self.id = "a"

    def __call__(self) -> str:
        return self.id


async def test_envelopes_count_from_one_in_put_order() -> None:
    outbox = Outbox(Open())
    await outbox.put(ONE)
    outbox.put_nowait(TWO)

    assert [await outbox.envelope(), await outbox.envelope()] == [
        EventEnvelope(seq=1, session="a", payload=ONE),
        EventEnvelope(seq=2, session="a", payload=TWO),
    ]


async def test_an_event_keeps_the_session_open_when_it_was_put() -> None:
    session = Open()
    outbox = Outbox(session)
    await outbox.put(ONE)
    session.id = "b"
    await outbox.put(TWO)

    assert (await outbox.envelope()).session == "a"
    assert (await outbox.envelope()).session == "b"


async def test_a_sessionless_event_belongs_to_no_session() -> None:
    outbox = Outbox(Open())
    await outbox.put(SessionsListed(sessions=(), reason="asked"))

    assert (await outbox.envelope()).session is None


async def test_envelope_waits_for_an_event_to_be_put() -> None:
    outbox = Outbox(Open())
    waiting = asyncio.create_task(outbox.envelope())
    await asyncio.sleep(0)
    outbox.put_nowait(ONE)

    assert await waiting == EventEnvelope(seq=1, session="a", payload=ONE)


def test_an_outbox_reads_events_only_in_their_envelopes() -> None:
    public = {name for name in dir(Outbox) if not name.startswith("_")}

    assert public == {
        "put",
        "put_nowait",
        "envelope",
        "transcript",
        "history",
        "missed",
        "resend",
        "close",
    }


async def test_with_a_log_envelopes_count_on_from_its_highest_seq() -> None:
    log = FakeStore()
    log.seq = 41
    outbox = Outbox(Open(), log)
    outbox.put_nowait(ONE)

    assert await outbox.envelope() == EventEnvelope(seq=42, session="a", payload=ONE)
    await outbox.close()


async def test_the_log_gets_a_sessions_events_but_for_those_answering_one_connection() -> None:
    log = FakeStore()
    outbox = Outbox(Open(), log)
    outbox.put_nowait(Welcome(command_id="hello", server_version="1"))
    outbox.put_nowait(SessionsListed(sessions=(), reason="asked"))
    outbox.put_nowait(ONE)
    await outbox.close()

    assert log.logged == [EventEnvelope(seq=3, session="a", payload=ONE)]


async def test_closing_an_outbox_without_a_log_does_nothing() -> None:
    outbox = Outbox(Open())
    outbox.put_nowait(ONE)
    await outbox.close()

    assert await outbox.envelope() == EventEnvelope(seq=1, session="a", payload=ONE)


async def test_the_logged_copy_has_its_long_fields_cut_while_the_live_one_is_whole() -> None:
    log = FakeStore()
    outbox = Outbox(Open(), log, cut_bytes=1024)
    output = "a" * 2500 + "b" * 2500
    outbox.put_nowait(ToolCallFinished(turn_id="t", call_id="c", output=output))
    await outbox.close()

    live = (await outbox.envelope()).payload
    [logged] = log.logged
    assert isinstance(live, ToolCallFinished) and live.output == output
    assert logged.seq == 1
    assert logged.payload == ToolCallFinished(
        turn_id="t",
        call_id="c",
        output="a" * 512 + "\n… 4 KB cut by orbit's tool_output_kb …\n" + "b" * 512,
    )


async def test_by_default_the_logged_copy_is_whole() -> None:
    log = FakeStore()
    outbox = Outbox(Open(), log)
    event = ToolCallFinished(turn_id="t", call_id="c", output="x" * 100_000)
    outbox.put_nowait(event)
    await outbox.close()

    assert log.logged == [EventEnvelope(seq=1, session="a", payload=event)]
