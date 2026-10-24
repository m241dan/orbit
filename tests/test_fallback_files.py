"""Fallback files: a session's events parked on disk while the store can't take them."""

import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from orbit.agent.application.ports import ParkedFile, ReadFailed, StoreFailed
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.protocol import EventEnvelope, decode_event
from tests.agent_fakes import notice


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
    path = tmp_path / "repo" / ".git" / "orbit" / "fallback" / "w1-s1.jsonl"

    assert await files.write("s1", [notice(1)]) == str(path)
    assert decoded(path) == [notice(1)]


async def test_events_stay_in_the_file_they_went_to_though_another_place_frees_up(
    files: FallbackFiles, tmp_path: Path, unwritable: Path
) -> None:
    await files.write("s1", [notice(1)])
    unwritable.chmod(0o700)
    path = tmp_path / "repo" / ".git" / "orbit" / "fallback" / "w1-s1.jsonl"

    assert await files.write("s1", [notice(2)]) == str(path)
    assert decoded(path) == [notice(1), notice(2)]


async def test_when_the_file_used_fails_the_next_place_is_tried(
    files: FallbackFiles, tmp_path: Path
) -> None:
    await files.write("s1", [notice(1)])
    (tmp_path / "state" / "fallback" / "w1-s1.jsonl").chmod(0o400)
    written = await files.write("s1", [notice(2)])

    assert written == str(tmp_path / "repo" / ".git" / "orbit" / "fallback" / "w1-s1.jsonl")


async def test_an_append_that_fails_midway_leaves_the_file_as_it_was_and_moves_on(
    files: FallbackFiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await files.write("s1", [notice(1)])
    real_write = os.write
    calls: list[int] = []

    def full_disk(descriptor: int, data: bytes) -> int:
        """Writes half of the first chunk, then finds the disk full, then writes as usual."""
        calls.append(descriptor)
        if len(calls) == 1:
            return real_write(descriptor, bytes(data)[: len(data) // 2])
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "write", full_disk)
    written = await files.write("s1", [notice(2), notice(3)])
    monkeypatch.undo()

    state = tmp_path / "state" / "fallback" / "w1-s1.jsonl"
    repository = tmp_path / "repo" / ".git" / "orbit" / "fallback" / "w1-s1.jsonl"
    assert written == str(repository)
    assert decoded(state) == [notice(1)]
    assert decoded(repository) == [notice(2), notice(3)]


async def test_when_neither_place_can_be_written_there_is_no_file(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so nothing can be made under it")
    files = FallbackFiles(blocked / "state", "w1", blocked / "repo")

    assert await files.write("s1", [notice(1)]) is None


async def test_removing_a_sessions_files_deletes_them_in_both_places(
    files: FallbackFiles, tmp_path: Path
) -> None:
    await files.write("s1", [notice(1)])
    repository = tmp_path / "repo" / ".git" / "orbit" / "fallback" / "w1-s1.jsonl"
    repository.parent.mkdir(parents=True)
    repository.write_text("")
    await files.write("s2", [notice(2)])
    await files.remove("s1")

    assert [Path(file.path).name for file in await files.parked()] == ["w1-s2.jsonl"]


async def test_a_file_that_cant_be_removed_is_left(files: FallbackFiles, tmp_path: Path) -> None:
    await files.write("s1", [notice(1)])
    directory = tmp_path / "state" / "fallback"
    directory.chmod(0o500)
    try:
        await files.remove("s1")
    finally:
        directory.chmod(0o700)

    assert (directory / "w1-s1.jsonl").exists()


async def test_after_removing_a_file_the_next_events_start_from_the_first_place(
    files: FallbackFiles, tmp_path: Path, unwritable: Path
) -> None:
    await files.write("s1", [notice(1)])
    await files.remove("s1")
    unwritable.chmod(0o700)

    assert await files.write("s1", [notice(2)]) == str(
        tmp_path / "state" / "fallback" / "w1-s1.jsonl"
    )


async def test_the_files_parked_are_this_workspaces_in_the_state_directory_then_the_repository(
    files: FallbackFiles, tmp_path: Path
) -> None:
    other = FallbackFiles(tmp_path / "state", "w2", tmp_path / "elsewhere")
    await other.write("s9", [notice(9)])
    await files.write("b-1", [notice(1)])
    await files.write("a", [notice(2)])
    repository = tmp_path / "repo" / ".git" / "orbit" / "fallback"
    repository.mkdir(parents=True)
    (repository / "w1-s3.jsonl").write_text("")
    (repository / "notes.txt").write_text("")
    state = tmp_path / "state" / "fallback"
    (state / "w1-a.jsonl").rename(repository / "w1-a.jsonl")

    assert await files.parked() == [
        ParkedFile(str(state / "w1-b-1.jsonl"), "b-1"),
        ParkedFile(str(repository / "w1-a.jsonl"), "a"),
        ParkedFile(str(repository / "w1-s3.jsonl"), "s3"),
    ]


async def test_a_repository_file_is_named_with_the_workspace(
    files: FallbackFiles, tmp_path: Path, unwritable: Path
) -> None:
    written = await files.write("s1", [notice(1)])

    assert written == str(tmp_path / "repo" / ".git" / "orbit" / "fallback" / "w1-s1.jsonl")


async def test_a_workspace_sharing_the_repository_parks_none_of_anothers_files(
    files: FallbackFiles, tmp_path: Path, unwritable: Path
) -> None:
    await files.write("s1", [notice(1)])
    unwritable.chmod(0o700)
    await files.write("s2", [notice(2)])
    other = FallbackFiles(tmp_path / "state", "w2", tmp_path / "repo" / ".git" / "orbit")

    assert len(await files.parked()) == 2
    assert await other.parked() == []


async def test_a_legacy_repository_file_is_parked_by_its_session_as_legacy(
    files: FallbackFiles, tmp_path: Path
) -> None:
    repository = tmp_path / "repo" / ".git" / "orbit" / "fallback"
    repository.mkdir(parents=True)
    (repository / "s3.jsonl").write_text("")

    assert await files.parked() == [ParkedFile(str(repository / "s3.jsonl"), "s3", legacy=True)]


async def test_with_no_fallback_directories_nothing_is_parked(files: FallbackFiles) -> None:
    assert await files.parked() == []


async def test_a_parked_file_reads_back_its_envelopes_but_those_of_unknown_types(
    files: FallbackFiles,
) -> None:
    await files.write("s1", [notice(1), notice(2)])
    [file] = await files.parked()
    with open(file.path, "a") as appended:
        appended.write('{"seq": 3, "session": "s1", "payload": {"type": "from_the_future"}}\n\n')

    assert await files.read(file) == [notice(1), notice(2)]


@pytest.mark.parametrize("text", ["not json\n", b"\xff\n", "not json\nnor this\n"])
async def test_a_parked_file_that_isnt_envelopes_cant_be_read(
    files: FallbackFiles, tmp_path: Path, text: str | bytes
) -> None:
    path = tmp_path / "bad.jsonl"
    if isinstance(text, str):
        path.write_text(text)
    else:
        path.write_bytes(text)

    with pytest.raises(ReadFailed):
        await files.read(ParkedFile(str(path), "s1"))


@pytest.mark.parametrize("torn", ['{"seq": 3, "sess', '{"seq": 3}', b"\xe2\x82"])
async def test_a_parked_file_ending_in_a_torn_line_is_read_without_it(
    files: FallbackFiles, torn: str | bytes
) -> None:
    await files.write("s1", [notice(1), notice(2)])
    [file] = await files.parked()
    with open(file.path, "ab") as appended:
        appended.write(torn.encode() if isinstance(torn, str) else torn)

    assert await files.read(file) == [notice(1), notice(2)]


async def test_a_parked_file_ending_in_a_whole_line_without_a_newline_reads_it(
    files: FallbackFiles,
) -> None:
    await files.write("s1", [notice(1), notice(2)])
    [file] = await files.parked()
    path = Path(file.path)
    path.write_text(path.read_text().removesuffix("\n"))

    assert await files.read(file) == [notice(1), notice(2)]


async def test_a_parked_file_with_a_torn_line_before_good_ones_reads_every_good_one(
    files: FallbackFiles,
) -> None:
    await files.write("s1", [notice(1)])
    [file] = await files.parked()
    with open(file.path, "a") as appended:
        appended.write('{"seq": 2, "sess\n')
    await files.write("s1", [notice(3)])

    assert await files.read(file) == [notice(1), notice(3)]


@pytest.mark.parametrize("torn", ['{"seq": 2, "sess', b"\xe2\x82"])
async def test_an_append_after_a_torn_last_line_reads_back_every_whole_envelope(
    files: FallbackFiles, torn: str | bytes
) -> None:
    await files.write("s1", [notice(1)])
    [file] = await files.parked()
    with open(file.path, "ab") as appended:
        appended.write(torn.encode() if isinstance(torn, str) else torn)
    await files.write("s1", [notice(3), notice(4)])

    assert await files.read(file) == [notice(1), notice(3), notice(4)]


async def test_an_append_after_a_line_without_a_newline_starts_a_line_of_its_own(
    files: FallbackFiles,
) -> None:
    await files.write("s1", [notice(1)])
    [file] = await files.parked()
    path = Path(file.path)
    path.write_text(path.read_text().removesuffix("\n"))
    await files.write("s1", [notice(2)])

    assert decoded(path) == [notice(1), notice(2)]


async def test_after_a_failed_append_that_cant_be_undone_the_next_envelopes_are_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so no repository file can be made under it")
    files = FallbackFiles(tmp_path / "state", "w1", blocked / "repo")
    await files.write("s1", [notice(1)])
    real_write = os.write
    calls: list[int] = []

    def full_disk(descriptor: int, data: bytes) -> int:
        """Writes half of the first chunk, then finds the disk full, then writes as usual."""
        calls.append(descriptor)
        if len(calls) == 1:
            return real_write(descriptor, bytes(data)[: len(data) // 2])
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(descriptor, data)

    def stuck(descriptor: int, length: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "write", full_disk)
    monkeypatch.setattr(os, "ftruncate", stuck)
    assert await files.write("s1", [notice(2), notice(3)]) is None
    monkeypatch.undo()
    state = tmp_path / "state" / "fallback" / "w1-s1.jsonl"

    assert await files.write("s1", [notice(4)]) == str(state)
    assert await files.read(ParkedFile(str(state), "s1")) == [notice(1), notice(2), notice(4)]


async def test_a_missing_parked_file_cant_be_read(files: FallbackFiles, tmp_path: Path) -> None:
    with pytest.raises(ReadFailed, match="No such file"):
        await files.read(ParkedFile(str(tmp_path / "gone.jsonl"), "s1"))


async def test_deleting_a_parked_file_removes_it(files: FallbackFiles) -> None:
    await files.write("s1", [notice(1)])
    [file] = await files.parked()
    await files.delete(file)

    assert await files.parked() == []


async def test_a_parked_file_that_cant_be_deleted_says_why(
    files: FallbackFiles, tmp_path: Path
) -> None:
    await files.write("s1", [notice(1)])
    [file] = await files.parked()
    directory = tmp_path / "state" / "fallback"
    directory.chmod(0o500)
    try:
        with pytest.raises(StoreFailed, match="Permission denied"):
            await files.delete(file)
    finally:
        directory.chmod(0o700)
