"""Consultations in the conversation, without the window showing them: the asker's tab notes the
consultation asked and its place in line, then its reply, which enter opens in the detail
pane, or why it was interrupted."""

from orbit.protocol import (
    EXPERIMENTER,
    PLANNER,
    AgentEntry,
    ConsultationAnswered,
    ConsultationAsked,
)
from tests.frontend_fakes import Shell
from tests.session_apps import with_agents
from tests.transcript_fakes import Session

RULE = "─" * 40
LABELS = {EXPERIMENTER: "Experimenter"}


def asked(position: int, number: int = 1) -> ConsultationAsked:
    return ConsultationAsked(
        consultation=number,
        asker=PLANNER,
        consultant=EXPERIMENTER,
        title="Check X",
        position=position,
    )


def answered(reply: str, interrupted: str | None = None) -> ConsultationAnswered:
    return ConsultationAnswered(
        consultation=1,
        asker=PLANNER,
        consultant=EXPERIMENTER,
        title="Check X",
        reply=reply,
        interrupted=interrupted,
    )


def texts(session: Session, key: str | None) -> list[str]:
    return [entry.text for entry in session.conversation.entries(key)]


def test_a_consultation_asked_shows_its_place_in_line_until_it_runs_next() -> None:
    session = Session(labels=LABELS)
    session.events(asked(2))
    assert texts(session, PLANNER) == ["→ Asked Experimenter: Check X (2nd in line)"]
    session.events(asked(1))
    assert texts(session, PLANNER) == ["→ Asked Experimenter: Check X"]
    assert texts(session, None) == ["→ Asked Experimenter: Check X"]
    assert session.shown.labels == ["→ Asked Experimenter: Check X"]
    assert session.tabs.unread == [PLANNER, PLANNER]


def test_consultations_go_in_the_askers_tab_alone() -> None:
    session = Session(labels=LABELS)
    session.show_only(EXPERIMENTER, "experimenter")
    session.events(asked(2), asked(1), answered("3"))
    assert texts(session, EXPERIMENTER) == []
    assert len(texts(session, PLANNER)) == 2


def test_a_reply_shows_its_line_count_and_first_lines_and_enter_opens_it_whole() -> None:
    reply = "\n".join(f"line {n}" for n in range(1, 43))
    session = Session(labels=LABELS)
    session.events(asked(1), answered(reply))
    header = "↩ Experimenter's reply to 'Check X' · 42 lines"
    assert texts(session, PLANNER)[-1] == f"{header}\nline 1\nline 2\nline 3"
    session.activate()
    session.choose()
    pane = session.window.pane
    assert pane.title == f"Detail · {header} · y copy"
    assert pane.body == [RULE, *reply.split("\n")]


def test_an_interrupted_consultation_says_why_in_a_line() -> None:
    session = Session()
    session.events(asked(1), answered("", "it was cancelled"))
    assert texts(session, PLANNER)[-1] == (
        "↩ Experimenter's consultation 'Check X' was interrupted: it was cancelled"
    )
    assert session.conversation.entries(PLANNER)[-1].call is None


def test_a_replayed_consultation_updates_its_entry_and_marks_nothing_unread() -> None:
    session = Session(labels=LABELS)
    session.feed.replace([asked(2), asked(1)], ())
    assert texts(session, PLANNER) == ["→ Asked Experimenter: Check X"]
    assert session.tabs.unread == []


def test_earlier_consultations_update_their_own_entries_not_later_ones() -> None:
    session = Session(labels=LABELS)
    session.events(asked(1, number=2))
    session.feed.prepend([asked(2), asked(1)])
    session.window.settle()
    assert texts(session, PLANNER) == [
        "→ Asked Experimenter: Check X",
        "→ Asked Experimenter: Check X",
    ]
    session.events(asked(1, number=2))
    assert len(texts(session, PLANNER)) == 2


def test_a_consultation_pending_on_an_earlier_page_updates_its_entry() -> None:
    session = Session(labels=LABELS)
    session.feed.prepend([asked(2)])
    session.window.settle()
    session.events(asked(1))
    assert texts(session, PLANNER) == ["→ Asked Experimenter: Check X"]


def test_a_consultation_answered_live_stays_answered_after_paging_back() -> None:
    session = Session(labels=LABELS)
    session.events(answered("3"))
    session.feed.prepend([asked(2)])
    session.window.settle()
    before = texts(session, PLANNER)
    session.events(asked(1, number=2))
    assert texts(session, PLANNER)[:2] == before
    assert len(texts(session, PLANNER)) == 3


def test_a_consultation_shown_live_and_earlier_updates_the_live_entry() -> None:
    session = Session(labels=LABELS)
    session.events(asked(2))
    session.feed.prepend([asked(3)])
    session.window.settle()
    session.events(asked(1))
    assert texts(session, PLANNER) == [
        "→ Asked Experimenter: Check X (3rd in line)",
        "→ Asked Experimenter: Check X",
    ]


async def test_the_frontend_labels_consultants_as_the_catalog_lists_them() -> None:
    shell = Shell()
    catalog = with_agents(
        AgentEntry(name=EXPERIMENTER, label="Lab", conversational=True, lane=EXPERIMENTER)
    )
    await shell.feed(catalog, asked(1))
    assert shell.tab_entries(PLANNER) == ["→ Asked Lab: Check X"]
    await shell.feed(asked(1, number=2).model_copy(update={"consultant": "critic"}))
    assert shell.tab_entries(PLANNER)[-1] == "→ Asked critic: Check X"
