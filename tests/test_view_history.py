from orbit.protocol import (
    EventEnvelope,
    History,
    HistoryPage,
    Notice,
    SessionSummary,
    Settings,
    Snapshot,
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
