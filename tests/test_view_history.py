import json
from typing import Any

from orbit.protocol import (
    EventEnvelope,
    History,
    HistoryPage,
    Notice,
    SessionSummary,
    Settings,
    Snapshot,
    decode_event,
    encode,
)
from orbit.tui.view.history import Paging, earlier_request, loading, page_arrived, paging_from


def logged(*seqs: int, session: str = "s1") -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope(seq=seq, session=session, payload=Notice(message=f"n{seq}")) for seq in seqs
    )


def snapshot(*seqs: int) -> Snapshot:
    return Snapshot(
        session=SessionSummary(id="s1", name="auth"),
        settings=Settings(),
        model_choices=(),
        busy={},
        tasks=(),
        steps=(),
        review=None,
        proposals=(),
        agents=(),
        transcript=logged(*seqs),
    )


def test_a_snapshot_with_a_transcript_may_have_earlier_events() -> None:
    assert paging_from(snapshot(7, 9)) == Paging(session="s1", oldest=7, more=True)


def test_a_snapshot_without_a_transcript_has_nothing_earlier_to_ask_for() -> None:
    paging = paging_from(snapshot())
    assert paging == Paging(session="s1")
    assert earlier_request(paging) is None


def test_earlier_events_are_asked_for_before_the_oldest_shown() -> None:
    paging = paging_from(snapshot(7, 9))
    assert earlier_request(paging) == History(session="s1", before_seq=7)


def test_nothing_more_is_asked_for_while_a_page_loads_or_once_none_remain() -> None:
    paging = paging_from(snapshot(7, 9))
    assert earlier_request(loading(paging, True)) is None
    assert earlier_request(Paging(session="s1", oldest=7)) is None
    assert earlier_request(Paging()) is None


def test_a_page_asked_for_moves_the_oldest_shown_back_and_says_whether_more_remain() -> None:
    asked = loading(paging_from(snapshot(7, 9)), True)
    page = HistoryPage(session="s1", events=logged(3, 5), more=True)
    assert page_arrived(asked, page) == Paging(session="s1", oldest=3, more=True)
    last = HistoryPage(session="s1", events=logged(3, 5), more=False)
    assert page_arrived(asked, last) == Paging(session="s1", oldest=3, more=False)


def test_an_empty_page_ends_the_paging() -> None:
    asked = loading(paging_from(snapshot(7, 9)), True)
    page = HistoryPage(session="s1", events=(), more=True)
    assert page_arrived(asked, page) == Paging(session="s1", oldest=7, more=False)


def test_a_page_not_asked_for_or_of_another_session_is_ignored() -> None:
    paging = paging_from(snapshot(7, 9))
    page = HistoryPage(session="s1", events=logged(3), more=True)
    assert page_arrived(paging, page) is None
    other = HistoryPage(session="s2", events=logged(3, session="s2"), more=True)
    assert page_arrived(loading(paging, True), other) is None


def test_the_last_page_says_whether_earlier_events_were_trimmed() -> None:
    asked = loading(paging_from(snapshot(7, 9)), True)
    last = HistoryPage(session="s1", events=logged(3, 5), more=False, trimmed=True)
    assert page_arrived(asked, last) == Paging(session="s1", oldest=3, trimmed=True)
    empty = HistoryPage(session="s1", events=(), more=False, trimmed=True)
    assert page_arrived(asked, empty) == Paging(session="s1", oldest=7, trimmed=True)


def test_a_page_with_more_before_it_isnt_the_trimmed_end() -> None:
    asked = loading(paging_from(snapshot(7, 9)), True)
    page = HistoryPage(session="s1", events=logged(3, 5), more=True, trimmed=True)
    assert page_arrived(asked, page) == Paging(session="s1", oldest=3, more=True)


def unknown(*seqs: int) -> list[dict[str, Any]]:
    return [{"seq": seq, "payload": {"type": "something_new"}} for seq in seqs]


def over_the_wire[M: Snapshot | HistoryPage](
    message: M, field: str, envelopes: list[dict[str, Any]]
) -> M:
    """``message`` as decoded from the wire with ``envelopes`` as its ``field``."""
    data = json.loads(encode(EventEnvelope(seq=20, session="s1", payload=message)))
    data["payload"][field] = envelopes
    decoded = decode_event(json.dumps(data))
    assert decoded is not None and isinstance(decoded.payload, type(message))
    return decoded.payload


def test_a_page_of_only_dropped_events_moves_the_paging_back_and_keeps_more() -> None:
    asked = loading(paging_from(snapshot(7, 9)), True)
    page = over_the_wire(HistoryPage(session="s1", events=(), more=True), "events", unknown(3, 5))
    assert page.events == ()
    assert page_arrived(asked, page) == Paging(session="s1", oldest=3, more=True)


def test_a_snapshot_of_only_dropped_events_can_ask_for_earlier_ones() -> None:
    received = over_the_wire(snapshot(), "transcript", unknown(4, 6))
    assert received.transcript == ()
    paging = paging_from(received)
    assert paging == Paging(session="s1", oldest=4, more=True)
    assert earlier_request(paging) == History(session="s1", before_seq=4)


def test_a_page_empty_on_the_wire_ends_the_paging() -> None:
    asked = loading(paging_from(snapshot(7, 9)), True)
    page = over_the_wire(HistoryPage(session="s1", events=(), more=True), "events", [])
    assert page_arrived(asked, page) == Paging(session="s1", oldest=7, more=False)


def test_messages_built_in_process_page_from_their_first_event() -> None:
    assert snapshot(7, 9).oldest_seq == 7
    assert HistoryPage(session="s1", events=logged(3, 5), more=True).oldest_seq == 3
    assert HistoryPage(session="s1", events=(), more=True).oldest_seq is None
