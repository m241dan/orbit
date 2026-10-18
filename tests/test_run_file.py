"""The run file saying where a running ``orbit serve`` is."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orbit.agent.domain.server import RunningServer
from orbit.agent.external.run_file import RunFile, running_server
from orbit.agent.external.sqlite_store import SqliteStore


def test_the_run_file_holds_the_url_and_process_id_owner_only(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.write("ws://127.0.0.1:4567/", 42)
    assert run_file.path == tmp_path / "run" / "workspace-1.json"
    assert json.loads(run_file.path.read_text()) == {"url": "ws://127.0.0.1:4567/", "pid": 42}
    assert stat.S_IMODE(run_file.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(run_file.path.parent.stat().st_mode) == 0o700


def test_removing_the_run_file_leaves_none(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.write("ws://127.0.0.1:4567/", 42)
    run_file.remove()
    run_file.remove()
    assert not run_file.path.exists()


def test_reading_the_run_file_gives_the_server_it_names(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.write("ws://127.0.0.1:4567/", 42)
    assert run_file.read() == RunningServer(url="ws://127.0.0.1:4567/", pid=42)


def test_reading_no_run_file_gives_none(tmp_path: Path) -> None:
    assert RunFile(tmp_path, "workspace-1").read() is None


@pytest.mark.parametrize(
    "content",
    [b"not json", b'{"url": "ws://127.0.0.1:1/"}', b'{"url": "ws://x/", "pid": 0}', b"\xff"],
)
def test_reading_an_unreadable_run_file_gives_none(tmp_path: Path, content: bytes) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.path.parent.mkdir()
    run_file.path.write_bytes(content)
    assert run_file.read() is None


def test_reading_a_run_file_that_is_a_directory_gives_none(tmp_path: Path) -> None:
    run_file = RunFile(tmp_path, "workspace-1")
    run_file.path.mkdir(parents=True)
    assert run_file.read() is None


async def registered(state: Path, workspace: Path) -> str:
    """The id the store in ``state`` gives ``workspace``."""
    store = await SqliteStore.open(state, str(workspace))
    await store.close()
    return store.workspace_id


async def test_the_running_server_is_the_one_its_live_process_names(tmp_path: Path) -> None:
    RunFile(tmp_path, await registered(tmp_path, tmp_path)).write("ws://127.0.0.1:1/", os.getpid())
    assert running_server(tmp_path, str(tmp_path)) == RunningServer(
        url="ws://127.0.0.1:1/", pid=os.getpid()
    )


async def test_a_run_file_whose_process_is_gone_is_ignored(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    RunFile(tmp_path, await registered(tmp_path, tmp_path)).write("ws://127.0.0.1:1/", process.pid)
    assert running_server(tmp_path, str(tmp_path)) is None


async def test_a_workspace_without_a_run_file_has_no_running_server(tmp_path: Path) -> None:
    await registered(tmp_path, tmp_path)
    assert running_server(tmp_path, str(tmp_path)) is None


async def test_another_workspaces_run_file_is_not_this_ones(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    RunFile(tmp_path, await registered(tmp_path, other)).write("ws://127.0.0.1:1/", os.getpid())
    assert running_server(tmp_path, str(tmp_path)) is None
