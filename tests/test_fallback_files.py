"""Fallback files: a session's events parked on disk while the store can't take them."""

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from orbit.agent.external.fallback_files import FallbackFiles
from orbit.protocol import EventEnvelope, Notice, decode_event


def notice(seq: int) -> EventEnvelope:
    return EventEnvelope(seq=seq, session="s1", payload=Notice(message=f"n{seq}"))


@pytest.fixture
def files(tmp_path: Path) -> FallbackFiles:
    return FallbackFiles(tmp_path / "state", "w1", tmp_path / "repo" / ".git" / "orbit")


@pytest.fixture
def unwritable(tmp_path: Path) -> Iterator[Path]:
    """The state directory, readable but not writable."""
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    yield state
    state.chmod(0o700)


def decoded(path: Path) -> list[EventEnvelope | None]:
    return [decode_event(line) for line in path.read_text().splitlines()]


async def test_events_go_to_the_state_directory_one_envelope_per_line(
    files: FallbackFiles, tmp_path: Path
) -> None:
    path = tmp_path / "state" / "fallback" / "w1-s1.jsonl"

    assert await files.write("s1", [notice(1), notice(2)]) == str(path)
    assert await files.write("s1", [notice(3)]) == str(path)
    assert decoded(path) == [notice(1), notice(2), notice(3)]


async def test_the_file_and_its_directory_are_the_owners_alone(
    files: FallbackFiles, tmp_path: Path
) -> None:
    await files.write("s1", [notice(1)])

    path = tmp_path / "state" / "fallback" / "w1-s1.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_a_file_already_there_is_made_the_owners_alone(
    files: FallbackFiles, tmp_path: Path
) -> None:
    path = tmp_path / "state" / "fallback" / "w1-s1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    path.chmod(0o644)
    await files.write("s1", [notice(1)])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_with_the_state_directory_unwritable_events_go_to_the_repository(
    files: FallbackFiles, tmp_path: Path, unwritable: Path
) -> None:
    path = tmp_path / "repo" / ".git" / "orbit" / "fallback" / "s1.jsonl"

    assert await files.write("s1", [notice(1)]) == str(path)
    assert decoded(path) == [notice(1)]


async def test_events_stay_in_the_file_they_went_to_though_another_place_frees_up(
    files: FallbackFiles, tmp_path: Path, unwritable: Path
) -> None:
    await files.write("s1", [notice(1)])
    unwritable.chmod(0o700)
    path = tmp_path / "repo" / ".git" / "orbit" / "fallback" / "s1.jsonl"

    assert await files.write("s1", [notice(2)]) == str(path)
    assert decoded(path) == [notice(1), notice(2)]


async def test_when_the_file_used_fails_the_next_place_is_tried(
    files: FallbackFiles, tmp_path: Path
) -> None:
    await files.write("s1", [notice(1)])
    (tmp_path / "state" / "fallback" / "w1-s1.jsonl").chmod(0o400)
    written = await files.write("s1", [notice(2)])

    assert written == str(tmp_path / "repo" / ".git" / "orbit" / "fallback" / "s1.jsonl")


async def test_when_neither_place_can_be_written_there_is_no_file(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so nothing can be made under it")
    files = FallbackFiles(blocked / "state", "w1", blocked / "repo")

    assert await files.write("s1", [notice(1)]) is None
