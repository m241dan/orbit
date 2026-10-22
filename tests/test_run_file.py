"""The run file saying where a running ``orbit serve`` is."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orbit.agent.domain.server import LocalTls, RunningServer
from orbit.agent.external.run_file import RunFile, ServeLock, running_server


def test_the_run_file_holds_the_url_and_process_id_owner_only(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path)
    run_file.write("ws://127.0.0.1:4567/", 42)
    assert run_file.path == tmp_path / "run" / "server.json"
    assert json.loads(run_file.path.read_text()) == {"url": "ws://127.0.0.1:4567/", "pid": 42}
    assert stat.S_IMODE(run_file.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(run_file.path.parent.stat().st_mode) == 0o700


def test_removing_the_run_file_leaves_none(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path)
    run_file.write("ws://127.0.0.1:4567/", os.getpid())
    run_file.remove()
    run_file.remove()
    assert not run_file.path.exists()


def test_removing_leaves_a_run_file_naming_another_process(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path)
    run_file.write("ws://127.0.0.1:4567/", os.getpid() + 1)
    run_file.remove()
    assert run_file.read() == RunningServer(url="ws://127.0.0.1:4567/", pid=os.getpid() + 1)


def test_reading_the_run_file_gives_the_server_it_names(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path)
    run_file.write("ws://127.0.0.1:4567/", 42)
    assert run_file.read() == RunningServer(url="ws://127.0.0.1:4567/", pid=42)


def test_a_tls_servers_run_file_records_the_ca_and_hostname_to_verify_it_by(
    tmp_path: Path,
) -> None:
    run_file = RunFile(tmp_path)
    local = LocalTls(mutual=True, ca="/tls/ca.pem", hostname="localhost")
    run_file.write("wss://0.0.0.0:4567/", 42, local)
    assert json.loads(run_file.path.read_text()) == {
        "url": "wss://0.0.0.0:4567/",
        "pid": 42,
        "tls": {"mutual": True, "ca": "/tls/ca.pem", "hostname": "localhost"},
    }
    assert run_file.read() == RunningServer(url="wss://0.0.0.0:4567/", pid=42, tls=local)


def test_reading_no_run_file_gives_none(tmp_path: Path) -> None:
    assert RunFile(tmp_path).read() is None


@pytest.mark.parametrize(
    "content",
    [b"not json", b'{"url": "ws://127.0.0.1:1/"}', b'{"url": "ws://x/", "pid": 0}', b"\xff"],
)
def test_reading_an_unreadable_run_file_gives_none(tmp_path: Path, content: bytes) -> None:
    run_file = RunFile(tmp_path)
    run_file.path.parent.mkdir()
    run_file.path.write_bytes(content)
    assert run_file.read() is None


def test_reading_a_run_file_that_is_a_directory_gives_none(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path)
    run_file.path.mkdir(parents=True)
    assert run_file.read() is None


def test_the_running_server_is_the_one_its_live_process_names(tmp_path: Path) -> None:
    RunFile(tmp_path).write("ws://127.0.0.1:1/", os.getpid())
    assert running_server(tmp_path) == RunningServer(url="ws://127.0.0.1:1/", pid=os.getpid())


def test_a_run_file_whose_process_is_gone_is_ignored(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    RunFile(tmp_path).write("ws://127.0.0.1:1/", process.pid)
    assert running_server(tmp_path) is None


def test_a_state_directory_without_a_run_file_has_no_running_server(tmp_path: Path) -> None:
    assert running_server(tmp_path) is None


def test_the_first_serve_takes_the_lock_owner_only(tmp_path: Path) -> None:
    lock = ServeLock(tmp_path)
    assert lock.take()
    assert lock.path == tmp_path / "run" / "serve.lock"
    assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.path.parent.stat().st_mode) == 0o700
    lock.close()


def test_a_second_serve_cant_take_the_lock_until_the_first_lets_go(tmp_path: Path) -> None:
    first, second = ServeLock(tmp_path), ServeLock(tmp_path)
    assert first.take()
    assert not second.take()
    first.close()
    first.close()
    assert second.take()
    second.close()


def test_the_lock_is_free_once_its_serves_process_ends(tmp_path: Path) -> None:
    script = (
        "import sys; from pathlib import Path;"
        " from orbit.agent.external.run_file import ServeLock;"
        " assert ServeLock(Path(sys.argv[1])).take()"
    )
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True)
    lock = ServeLock(tmp_path)
    assert lock.take()
    lock.close()


def test_a_lock_that_cant_be_made_raises(tmp_path: Path) -> None:
    (tmp_path / "run").write_text("a file, not a directory")
    with pytest.raises(OSError):
        ServeLock(tmp_path).take()


def test_a_lock_that_cant_be_taken_raises_and_stays_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(fd: int, operation: int) -> None:
        raise OSError("no locks here")

    lock = ServeLock(tmp_path)
    monkeypatch.setattr("orbit.agent.external.file_locks.fcntl.flock", failing)
    with pytest.raises(OSError, match="no locks here"):
        lock.take()
    monkeypatch.undo()
    assert lock.take()
    lock.close()


def test_taking_the_lock_leaves_its_file_empty(tmp_path: Path) -> None:
    lock = ServeLock(tmp_path)
    assert lock.take()
    assert lock.path.read_bytes() == b""
    lock.close()
