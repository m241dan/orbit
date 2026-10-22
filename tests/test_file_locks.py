"""File locks: an exclusive flock on a file that names whoever holds it."""

import os
import stat
from pathlib import Path

import pytest

from orbit.agent.external.file_locks import rewrite, take_flock


def test_taking_a_lock_writes_the_text_over_longer_content(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    lock.write_text("a much longer earlier holder")
    fd = take_flock(lock, "123")
    assert fd is not None
    assert lock.read_text() == "123"
    os.close(fd)


def test_a_second_take_is_refused_and_leaves_the_holders_text(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    fd = take_flock(lock, "first")
    assert fd is not None
    assert take_flock(lock, "second") is None
    assert lock.read_text() == "first"
    os.close(fd)


def test_another_failure_closes_the_descriptor_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def recording_open(path: Path, flags: int, mode: int) -> int:
        opened.append(real_open(path, flags, mode))
        return opened[-1]

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def fail(fd: int, length: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched, pytest.raises(OSError, match="disk full"):
        patched.setattr(os, "open", recording_open)
        patched.setattr(os, "close", recording_close)
        patched.setattr(os, "ftruncate", fail)
        take_flock(tmp_path / "a.lock", "123")
    assert closed == opened


def test_rewriting_finishes_a_partial_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock = tmp_path / "a.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    real_pwrite = os.pwrite

    def one_byte(fd: int, data: bytes, offset: int) -> int:
        return real_pwrite(fd, data[:1], offset)

    monkeypatch.setattr(os, "pwrite", one_byte)
    rewrite(fd, "hello")
    monkeypatch.undo()
    os.close(fd)
    assert lock.read_text() == "hello"


def test_a_new_lock_file_takes_the_mode_its_given(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    fd = take_flock(lock, "123", 0o600)
    assert fd is not None
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    os.close(fd)
