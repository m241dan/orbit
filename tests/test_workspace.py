import os
import threading
from pathlib import Path
from typing import Any

import pytest

from orbit.agent.external.workspace import workspace_state
from tests.git_repo import git, init_repo


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """A repository with nothing committed, its ``.gitignore`` and ``tracked.py`` staged."""
    root = init_repo(tmp_path / "staged")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "tracked.py").write_text("x = 1\n")
    git(root, "add", ".")
    return root


async def test_unchanged_tree_has_the_same_state(staged: Path) -> None:
    assert await workspace_state(staged) == await workspace_state(staged)


async def test_editing_a_tracked_file_changes_the_state(staged: Path) -> None:
    before = await workspace_state(staged)
    (staged / "tracked.py").write_text("x = 2  # longer\n")
    assert await workspace_state(staged) != before


async def test_touching_a_file_changes_the_state(staged: Path) -> None:
    before = await workspace_state(staged)
    stat = (staged / "tracked.py").stat()
    os.utime(staged / "tracked.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert await workspace_state(staged) != before


async def test_new_and_deleted_files_change_the_state(staged: Path) -> None:
    before = await workspace_state(staged)
    (staged / "new.py").write_text("")
    with_new = await workspace_state(staged)
    assert with_new != before

    (staged / "tracked.py").unlink()
    assert await workspace_state(staged) not in (before, with_new)


async def test_ignored_files_do_not_count(staged: Path) -> None:
    before = await workspace_state(staged)
    (staged / "ignored").mkdir()
    (staged / "ignored" / "cache.bin").write_text("noise")
    assert await workspace_state(staged) == before


async def test_outside_a_git_work_tree_the_state_is_unknown(tmp_path: Path) -> None:
    assert await workspace_state(tmp_path) is None


async def test_without_git_the_state_is_unknown(
    staged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    assert await workspace_state(staged) is None


async def test_a_missing_tracked_file_has_no_size_or_time(staged: Path) -> None:
    (staged / "tracked.py").unlink()
    state = await workspace_state(staged)
    assert state is not None
    assert ("tracked.py", -1, -1) in state


async def test_files_are_checked_off_the_event_loops_thread(
    staged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: set[threading.Thread] = set()
    real_stat = os.stat

    def recording_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        threads.add(threading.current_thread())
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", recording_stat)
    state = await workspace_state(staged)
    assert state is not None
    assert {path for path, _, _ in state} == {".gitignore", "tracked.py"}
    assert threads
    assert threading.current_thread() not in threads
