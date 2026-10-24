"""Paging back through the open session's history, without the conversation showing it: which
page to ask for and when, what it shows while one loads, and what it makes of each answer."""

from collections.abc import Iterable

from orbit.protocol import (
    PLANNER_LANE,
    STORAGE_FAILED,
    WORK_LANE,
    CommandRefused,
    ErrorOccurred,
    Event,
    EventEnvelope,
    History,
    HistoryPage,
    Notice,
    SessionSummary,
    Settings,
    Snapshot,
)
from orbit.tui.view.history import HistoryPager
from tests.desk_fakes import Backend


def logged(first: int, *messages: str) -> tuple[EventEnvelope, ...]:
    """Notices of ``messages`` as the session ``s1`` logged them, numbered from ``first``."""
    return tuple(
        EventEnvelope(seq=seq, session="s1", payload=Notice(message=message))
        for seq, message in enumerate(messages, start=first)
    )


def snapshot(*transcript: EventEnvelope, session: str = "s1") -> Snapshot:
    return Snapshot(
        session=SessionSummary(id=session, name="auth rework"),
        settings=Settings(),
        model_choices=(),
        busy={WORK_LANE: False, PLANNER_LANE: False},
        tasks=(),
        steps=(),
        review=None,
        proposals=(),
        agents=(),
        transcript=transcript,
    )


def page(
    *events: EventEnvelope, more: bool = False, session: str = "s1", trimmed: bool = False
) -> HistoryPage:
    return HistoryPage(session=session, events=events, more=more, trimmed=trimmed)


class Conversation:
    """The conversation the pager shows pages in: whether it shows that earlier events load or
    were trimmed, and the messages of the notices shown above it, a list each page."""

    def __init__(self) -> None:
        self.loading = False
        self.trimmed = False
        self.pages: list[list[str]] = []

    def show_loading(self, loading: bool) -> None:
        self.loading = loading

    def show_trimmed(self, trimmed: bool) -> None:
        self.trimmed = trimmed

    def prepend(self, events: Iterable[Event]) -> None:
        self.pages.append([event.message for event in events if isinstance(event, Notice)])


def histories(backend: Backend) -> list[History]:
    return [command for command in backend.commands if isinstance(command, History)]


def paging(*transcript: EventEnvelope) -> tuple[HistoryPager, Conversation, Backend]:
    """A pager following a snapshot of ``transcript``, shown in a conversation, asking through a
    backend."""
    conversation, backend = Conversation(), Backend()
    pager = HistoryPager(conversation, conversation, backend.desk.send)
    pager.follow(snapshot(*transcript))
    return pager, conversation, backend


async def test_asking_for_earlier_events_shows_the_page_above_once_it_comes() -> None:
    pager, conversation, backend = paging(*logged(501, "c", "d"))
    await pager.earlier()
    assert histories(backend) == [History(session="s1", before_seq=501)]
    assert conversation.loading
    await pager.earlier()
    assert len(histories(backend)) == 1  # One request at a time.
    pager.show(page(*logged(1, "a", "b"), more=True))
    assert conversation.pages == [["a", "b"]]
    assert not conversation.loading


async def test_with_no_more_earlier_events_asks_for_nothing() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    pager.show(page(*logged(1, "a"), more=False))
    await pager.earlier()
    assert histories(backend) == [History(session="s1", before_seq=501)]
    assert not conversation.loading


async def test_the_next_page_is_asked_for_before_the_oldest_shown() -> None:
    pager, _, backend = paging(*logged(501, "c"))
    await pager.earlier()
    pager.show(page(*logged(301, "b"), more=True))
    await pager.earlier()
    assert [history.before_seq for history in histories(backend)] == [501, 301]


async def test_an_empty_page_ends_the_paging() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    pager.show(page(more=True))
    await pager.earlier()
    assert len(histories(backend)) == 1 and conversation.pages == [[]]


async def test_an_empty_transcript_has_no_history_to_ask_for() -> None:
    pager, conversation, backend = paging()
    await pager.earlier()
    assert histories(backend) == [] and not conversation.loading


def test_a_page_not_asked_for_is_ignored() -> None:
    pager, conversation, _ = paging(*logged(501, "c"))
    pager.show(page(*logged(1, "a")))
    assert conversation.pages == []


async def test_a_snapshot_forgets_the_page_loading() -> None:
    pager, conversation, _ = paging(*logged(501, "c"))
    await pager.earlier()
    pager.follow(snapshot(*logged(601, "x"), session="s2"))
    assert not conversation.loading
    pager.show(page(*logged(1, "a")))
    assert conversation.pages == []


async def test_a_page_for_another_session_is_ignored() -> None:
    pager, conversation, _ = paging(*logged(501, "c"))
    await pager.earlier()
    pager.show(page(*logged(1, "a"), session="s2"))
    assert conversation.pages == [] and conversation.loading


async def test_the_last_page_of_a_trimmed_log_shows_older_events_were_removed() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    assert not conversation.trimmed
    pager.show(page(*logged(400, "b"), trimmed=True))
    assert conversation.trimmed and conversation.pages == [["b"]]
    await pager.earlier()
    assert len(histories(backend)) == 1


async def test_a_page_with_more_to_come_doesnt_show_older_events_were_removed() -> None:
    pager, conversation, _ = paging(*logged(501, "c"))
    await pager.earlier()
    pager.show(page(*logged(400, "b"), more=True, trimmed=True))
    assert not conversation.trimmed


async def test_a_snapshot_hides_the_removed_events_line() -> None:
    pager, conversation, _ = paging(*logged(501, "c"))
    await pager.earlier()
    pager.show(page(trimmed=True))
    assert conversation.trimmed
    pager.follow(snapshot(*logged(601, "x")))
    assert not conversation.trimmed


async def test_a_request_that_fails_to_send_can_be_made_again() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    backend.send_error = RuntimeError("gone")
    await pager.earlier()
    assert not conversation.loading
    assert backend.shown == [
        ErrorOccurred(code="internal", message="Couldn't send the command: gone")
    ]
    backend.send_error = None
    await pager.earlier()
    assert len(histories(backend)) == 2 and conversation.loading


async def test_a_refused_request_stops_loading_and_can_be_made_again() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    refusal = backend.refuse("Couldn't read.", code="internal")
    assert not conversation.loading and backend.shown == [refusal]
    await pager.earlier()
    assert histories(backend) == [History(session="s1", before_seq=501)] * 2
    assert conversation.loading


async def test_a_request_refused_for_storage_stops_loading_and_can_be_made_again() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    backend.refuse("Storage failed.", code=STORAGE_FAILED)
    assert not conversation.loading
    await pager.earlier()
    assert histories(backend) == [History(session="s1", before_seq=501)] * 2
    assert conversation.loading


async def test_a_refusal_of_an_older_request_leaves_the_newer_loading() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    pager.follow(snapshot(*logged(501, "c")))
    await pager.earlier()
    assert len(histories(backend)) == 2
    backend.refuse("Couldn't read.", code="internal", index=0)
    assert conversation.loading


async def test_a_refusal_of_another_command_leaves_the_page_loading() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    backend.desk.answer(CommandRefused(command_id="other", code="internal", message="No."))
    assert conversation.loading
    await pager.earlier()
    assert len(histories(backend)) == 1


async def test_a_page_arriving_after_its_request_was_refused_is_ignored() -> None:
    pager, conversation, backend = paging(*logged(501, "c"))
    await pager.earlier()
    backend.refuse("Couldn't read.", code="internal")
    pager.show(page(*logged(1, "a")))
    assert conversation.pages == [] and not conversation.loading
