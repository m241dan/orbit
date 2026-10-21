"""Running git, against real throwaway git repositories."""

from pathlib import Path

import pytest

from orbit.agent.external.git import GitResult, GitUnavailable, git, git_sync


def test_a_result_is_ok_only_when_git_exits_zero() -> None:
    assert GitResult(0, b"").ok
    assert not GitResult(1, b"").ok


def test_a_results_text_is_decoded_and_stripped() -> None:
    assert GitResult(0, b"  main\xff\n").text == "main�"


async def test_git_gives_what_it_exits_with_and_prints(repo: Path) -> None:
    result = await git(repo, "symbolic-ref", "--short", "HEAD")
    assert result == GitResult(0, b"main\n")


async def test_a_failing_command_gives_its_status_and_message(repo: Path) -> None:
    result = await git(repo, "rev-parse", "--verify", "no-such-ref")
    assert result.status == 128 and result.text.startswith("fatal:")


async def test_without_stderr_only_what_git_prints_to_stdout_is_given(repo: Path) -> None:
    assert await git(repo, "rev-parse", "--verify", "no-such-ref", stderr=False) == GitResult(
        128, b""
    )


def test_git_sync_gives_the_same_as_git(repo: Path) -> None:
    assert git_sync(repo, "symbolic-ref", "--short", "HEAD") == GitResult(0, b"main\n")
    assert git_sync(repo, "rev-parse", "--verify", "no-such-ref").text.startswith("fatal:")
    assert git_sync(repo, "rev-parse", "--verify", "no-such-ref", stderr=False).output == b""


async def test_a_missing_directory_makes_git_unavailable(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(GitUnavailable, match=f"git status failed to start in {missing}"):
        await git(missing, "status")
    with pytest.raises(GitUnavailable, match=f"git status failed to start in {missing}"):
        git_sync(missing, "status")


async def test_a_missing_git_makes_git_unavailable(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setenv("PATH", str(repo))
    with pytest.raises(GitUnavailable, match="git status failed to start"):
        await git(repo, "status")
    with pytest.raises(GitUnavailable, match="git status failed to start"):
        git_sync(repo, "status")


def test_git_unavailable_is_a_runtime_error() -> None:
    assert issubclass(GitUnavailable, RuntimeError)
