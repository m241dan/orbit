"""The outbox the session publishes into, stamping each event as it's put in."""

from orbit.agent.application.outbox import Outbox
from orbit.agent.domain.storage import StorageSettings
from orbit.protocol import EventEnvelope, Notice, ToolCallFinished, UsageChanged, Welcome
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
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open())
    outbox.deliver = sent.append
    await outbox.put(ONE)
    outbox.put_nowait(TWO)

    assert sent == [
        EventEnvelope(seq=1, session="a", payload=ONE),
        EventEnvelope(seq=2, session="a", payload=TWO),
    ]


async def test_an_event_keeps_the_session_open_when_it_was_put() -> None:
    session = Open()
    sent: list[EventEnvelope] = []
    outbox = Outbox(session)
    outbox.deliver = sent.append
    await outbox.put(ONE)
    session.id = "b"
    await outbox.put(TWO)

    assert [envelope.session for envelope in sent] == ["a", "b"]


async def test_a_sessionless_event_belongs_to_no_session() -> None:
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open())
    outbox.deliver = sent.append
    await outbox.put(UsageChanged())

    assert sent[0].session is None


async def test_an_event_names_the_workspace_but_for_the_control_planes_own() -> None:
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open())
    outbox.workspace = "w1"
    outbox.deliver = sent.append
    await outbox.put(ONE)
    await outbox.put(UsageChanged())
    await outbox.put(Welcome(command_id="hello", server_version="1"))

    assert [envelope.workspace for envelope in sent] == ["w1", None, None]


async def test_a_stamped_event_is_logged_but_not_delivered() -> None:
    log = FakeStore()
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    stamped = outbox.stamp(ONE)
    outbox.put_nowait(TWO)
    await outbox.close()

    assert stamped == EventEnvelope(seq=1, session="a", payload=ONE)
    assert sent == [EventEnvelope(seq=2, session="a", payload=TWO)]
    assert [envelope.seq for envelope in log.logged] == [1, 2]


def test_until_told_where_an_outbox_delivers_nothing_but_still_stamps() -> None:
    outbox = Outbox(Open())
    outbox.put_nowait(ONE)

    assert outbox.stamp(TWO) == EventEnvelope(seq=2, session="a", payload=TWO)


def test_an_outbox_delivers_events_only_in_their_envelopes() -> None:
    public = {name for name in dir(Outbox) if not name.startswith("_")}

    assert public == {
        "put",
        "put_nowait",
        "stamp",
        "last_seq",
        "catch_up",
        "transcript",
        "history",
        "missed",
        "forget",
        "stop_log",
        "resume_log",
        "close",
    }


async def test_with_a_log_envelopes_count_on_from_its_highest_seq() -> None:
    log = FakeStore()
    log.seq = 41
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    outbox.put_nowait(ONE)

    assert sent[0] == EventEnvelope(seq=42, session="a", payload=ONE)
    await outbox.close()


async def test_catching_up_counts_on_from_the_highest_seq_logged_since() -> None:
    log = FakeStore()
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    log.seq = 50
    await outbox.catch_up()
    outbox.put_nowait(ONE)

    assert sent[0].seq == 51
    await outbox.close()


async def test_catching_up_never_counts_down() -> None:
    log = FakeStore()
    log.seq = 50
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    log.seq = 10
    await outbox.catch_up()
    outbox.put_nowait(ONE)

    assert sent[0].seq == 51
    await outbox.close()


class Unread(FakeStore):
    """A log whose highest ``seq`` can't be read now."""

    async def latest_seq(self) -> int | None:
        return None


async def test_catching_up_on_a_log_that_cant_be_read_counts_on() -> None:
    log = Unread()
    log.seq = 5
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    await outbox.catch_up()
    outbox.put_nowait(ONE)

    assert sent[0].seq == 6
    await outbox.close()


async def test_catching_up_without_a_log_counts_on() -> None:
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open())
    outbox.deliver = sent.append
    outbox.put_nowait(ONE)
    await outbox.catch_up()
    outbox.put_nowait(TWO)

    assert [envelope.seq for envelope in sent] == [1, 2]


async def test_the_log_gets_a_sessions_events_but_for_those_answering_one_connection() -> None:
    log = FakeStore()
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    outbox.put_nowait(Welcome(command_id="hello", server_version="1"))
    outbox.put_nowait(UsageChanged())
    outbox.put_nowait(ONE)
    await outbox.close()

    assert log.logged == [EventEnvelope(seq=3, session="a", payload=ONE)]


async def test_closing_an_outbox_without_a_log_does_nothing() -> None:
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open())
    outbox.deliver = sent.append
    outbox.put_nowait(ONE)
    await outbox.close()

    assert sent[0] == EventEnvelope(seq=1, session="a", payload=ONE)


async def test_the_logged_copy_has_its_long_fields_cut_while_the_live_one_is_whole() -> None:
    log = FakeStore()
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log, StorageSettings(tool_output_kb=1, session_log_mb=0))
    outbox.deliver = sent.append
    output = "a" * 2500 + "b" * 2500
    outbox.put_nowait(ToolCallFinished(turn_id="t", call_id="c", output=output))
    await outbox.close()

    live = sent[0].payload
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
    sent: list[EventEnvelope] = []
    outbox = Outbox(Open(), log)
    outbox.deliver = sent.append
    event = ToolCallFinished(turn_id="t", call_id="c", output="x" * 100_000)
    outbox.put_nowait(event)
    await outbox.close()

    assert log.logged == [EventEnvelope(seq=1, session="a", payload=event)]
