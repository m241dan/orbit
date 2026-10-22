"""Who owns a repository: what its workspace lock says, and what a read-only orbit tells the
user."""

import pytest

from orbit.agent.domain.ownership import (
    WorkspaceOwner,
    owner_text,
    parse_owner,
    queued_dropped_note,
    read_only_note,
)


def test_an_owner_round_trips_through_its_lock_text() -> None:
    owner = WorkspaceOwner(pid=42, url="ws://127.0.0.1:7433/")
    assert parse_owner(owner_text(owner)) == owner


def test_a_chat_owner_has_no_url() -> None:
    assert parse_owner(owner_text(WorkspaceOwner(pid=42))) == WorkspaceOwner(pid=42)


@pytest.mark.parametrize("text", ["", "not json", '{"pid": 0}', '{"pid": "42"}'])
def test_lock_text_that_cant_be_read_names_no_owner(text: str) -> None:
    assert parse_owner(text) == WorkspaceOwner()


def test_an_orbit_serve_is_named_by_its_url() -> None:
    owner = WorkspaceOwner(pid=42, url="ws://127.0.0.1:7433/")
    assert owner.described == "an orbit serve at ws://127.0.0.1:7433/"


def test_an_orbit_chat_is_named_by_its_process_id() -> None:
    assert WorkspaceOwner(pid=42).described == "an orbit chat, pid 42"


def test_an_owner_that_cant_be_read_is_another_orbit() -> None:
    assert WorkspaceOwner().described == "another orbit"


def test_the_read_only_note_names_the_owner_and_says_work_is_queued() -> None:
    assert read_only_note(WorkspaceOwner(pid=42)) == (
        "Read-only: an orbit chat, pid 42 owns this repository. Tasks you run are queued until"
        " this orbit owns it."
    )


def test_the_dropped_note_counts_one_queued_command() -> None:
    assert queued_dropped_note(1) == (
        "Switched sessions: 1 queued command was dropped. Ask for it again in this session."
    )


def test_the_dropped_note_counts_several_queued_commands() -> None:
    assert queued_dropped_note(2) == (
        "Switched sessions: 2 queued commands were dropped. Ask for them again in this session."
    )
