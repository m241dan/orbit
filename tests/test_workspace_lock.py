"""The workspace lock: the first orbit to reach a repository owns it until its process ends."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.external.workspace_lock import WorkspaceLock

URL = "ws://127.0.0.1:7433/"


def test_the_first_orbit_takes_the_lock_naming_its_process(tmp_path: Path) -> None:
    lock = WorkspaceLock(tmp_path / "orbit")
    assert lock.take() is None
    assert lock.held
    assert lock.path == tmp_path / "orbit" / "workspace.lock"
    assert json.loads(lock.path.read_text()) == {"pid": os.getpid()}
    lock.close()


def test_the_lock_file_is_readable_by_its_owner_alone(tmp_path: Path) -> None:
    lock = WorkspaceLock(tmp_path)
    assert lock.take() is None
    assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
    lock.close()


def test_a_second_orbit_is_refused_and_told_the_owner(tmp_path: Path) -> None:
    first, second = WorkspaceLock(tmp_path), WorkspaceLock(tmp_path)
    first.take()
    assert second.take() == WorkspaceOwner(pid=os.getpid())
    assert not second.held
    first.close()


def test_an_orbit_serve_names_its_url_once_listening(tmp_path: Path) -> None:
    first, second = WorkspaceLock(tmp_path), WorkspaceLock(tmp_path)
    first.take()
    first.announce(URL)
    assert second.take() == WorkspaceOwner(pid=os.getpid(), url=URL)
    first.close()


def test_announcing_without_the_lock_leaves_the_owners_file_alone(tmp_path: Path) -> None:
    first, second = WorkspaceLock(tmp_path), WorkspaceLock(tmp_path)
    first.take()
    second.take()
    second.announce(URL)
    assert first.owner() == WorkspaceOwner(pid=os.getpid())
    first.close()


def test_an_orbit_serve_taking_the_lock_later_names_its_url(tmp_path: Path) -> None:
    first, second = WorkspaceLock(tmp_path), WorkspaceLock(tmp_path)
    first.take()
    second.take()
    second.announce(URL)
    first.close()
    assert second.take() is None
    assert first.owner() == WorkspaceOwner(pid=os.getpid(), url=URL)
    second.close()


def test_a_lock_let_go_of_can_be_taken_again(tmp_path: Path) -> None:
    first, second = WorkspaceLock(tmp_path), WorkspaceLock(tmp_path)
    first.take()
    first.close()
    first.close()
    assert not first.held
    assert second.take() is None
    second.close()


def test_the_lock_is_free_once_its_owners_process_ends(tmp_path: Path) -> None:
    script = (
        "import sys; from pathlib import Path;"
        " from orbit.agent.external.workspace_lock import WorkspaceLock;"
        " assert WorkspaceLock(Path(sys.argv[1])).take() is None"
    )
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True)
    lock = WorkspaceLock(tmp_path)
    assert lock.take() is None
    lock.close()


@pytest.mark.parametrize("content", [b"", b"not json", b"\xff"])
def test_a_lock_file_that_cant_be_read_names_another_orbit(tmp_path: Path, content: bytes) -> None:
    lock = WorkspaceLock(tmp_path)
    tmp_path.joinpath("workspace.lock").write_bytes(content)
    assert lock.owner() == WorkspaceOwner()


def test_no_lock_file_names_another_orbit(tmp_path: Path) -> None:
    assert WorkspaceLock(tmp_path).owner() == WorkspaceOwner()


def test_a_lock_that_cant_be_made_raises(tmp_path: Path) -> None:
    (tmp_path / "orbit").write_text("a file, not a directory")
    with pytest.raises(OSError):
        WorkspaceLock(tmp_path / "orbit").take()


def test_a_lock_that_cant_be_written_is_let_go_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(fd: int, length: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched, pytest.raises(OSError, match="disk full"):
        patched.setattr(os, "ftruncate", fail)
        WorkspaceLock(tmp_path).take()
    lock = WorkspaceLock(tmp_path)
    assert lock.take() is None
    lock.close()
