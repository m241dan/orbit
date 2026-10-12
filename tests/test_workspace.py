import os
import subprocess
from pathlib import Path

import pytest

from orbit.agent.workspace import workspace_state


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "tracked.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


async def test_unchanged_tree_has_the_same_state(repo: Path) -> None:
    assert await workspace_state(repo) == await workspace_state(repo)


async def test_editing_a_tracked_file_changes_the_state(repo: Path) -> None:
    before = await workspace_state(repo)
    (repo / "tracked.py").write_text("x = 2  # longer\n")
    assert await workspace_state(repo) != before


async def test_touching_a_file_changes_the_state(repo: Path) -> None:
    before = await workspace_state(repo)
    stat = (repo / "tracked.py").stat()
    os.utime(repo / "tracked.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert await workspace_state(repo) != before


async def test_new_and_deleted_files_change_the_state(repo: Path) -> None:
    before = await workspace_state(repo)
    (repo / "new.py").write_text("")
    with_new = await workspace_state(repo)
    assert with_new != before

    (repo / "tracked.py").unlink()
    assert await workspace_state(repo) not in (before, with_new)


async def test_ignored_files_do_not_count(repo: Path) -> None:
    before = await workspace_state(repo)
    (repo / "ignored").mkdir()
    (repo / "ignored" / "cache.bin").write_text("noise")
    assert await workspace_state(repo) == before


async def test_outside_a_git_work_tree_the_state_is_unknown(tmp_path: Path) -> None:
    assert await workspace_state(tmp_path) is None


async def test_without_git_the_state_is_unknown(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    assert await workspace_state(repo) is None
