"""Cloning repositories into the clone directory with git itself: named for their URLs,
reporting git's progress, never prompting, and leaving nothing behind when they fail or are
cancelled."""

import asyncio
from pathlib import Path

import pytest

from orbit.agent import CloneFailed, Cloner
from orbit.agent.domain.clones import AUTH_HINT
from orbit.agent.external.cloner import ssh_command
from orbit.protocol import WorkspaceCloning
from tests.git_repo import bare_repository, started
from tests.process_checks import gone


class Progress:
    """The progress a clone reported."""

    def __init__(self) -> None:
        self.events: list[WorkspaceCloning] = []

    def __call__(self, event: WorkspaceCloning) -> None:
        self.events.append(event)


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Git with no global or system configuration but the file returned, and no SSH command
    set in the environment."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    return config


async def test_a_repository_is_cloned_into_a_directory_named_for_it_reporting_progress(
    tmp_path: Path,
) -> None:
    url = bare_repository(tmp_path).as_uri()
    progress = Progress()

    path = await Cloner(tmp_path / "clones").clone(url, progress)

    assert path == str(tmp_path / "clones" / "proj")
    assert (Path(path) / "a.txt").read_text() == "one\n"
    assert WorkspaceCloning(url=url, phase="Receiving objects", percent=100) in progress.events
    assert len(progress.events) == len(set(progress.events))


async def test_a_name_taken_is_numbered(tmp_path: Path) -> None:
    url = bare_repository(tmp_path).as_uri()
    cloner = Cloner(tmp_path / "clones")

    first = await cloner.clone(url, Progress())
    second = await cloner.clone(str(tmp_path / "proj.git"), Progress())

    assert (Path(first).name, Path(second).name) == ("proj", "proj-2")


async def test_a_failed_clone_gives_gits_message_and_leaves_no_directory(
    tmp_path: Path,
) -> None:
    url = (tmp_path / "nothing.git").as_uri()
    clones = tmp_path / "clones"

    with pytest.raises(CloneFailed) as failed:
        await Cloner(clones).clone(url, Progress())

    assert str(failed.value).startswith(f"Couldn't clone {url}: ")
    assert "does not appear to be a git repository" in str(failed.value)
    assert list(clones.iterdir()) == []


async def test_a_cancelled_clone_stops_git_and_leaves_no_directory(
    tmp_path: Path, slow_ssh: Path
) -> None:
    clones = tmp_path / "clones"
    cloning = asyncio.create_task(
        Cloner(clones).clone("ssh://git@example.invalid/o/proj.git", Progress())
    )
    pid = await asyncio.wait_for(started(slow_ssh), 10)

    cloning.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cloning

    assert await gone(pid)
    assert list(clones.iterdir()) == []


async def test_a_clone_when_git_cant_start_fails_and_leaves_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    clones = tmp_path / "clones"

    with pytest.raises(CloneFailed, match="Couldn't clone /srv/proj: git failed to start: "):
        await Cloner(clones).clone("/srv/proj", Progress())

    assert list(clones.iterdir()) == []


async def test_a_clone_directory_that_cant_be_made_fails(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    clones = tmp_path / "file" / "clones"

    with pytest.raises(
        CloneFailed, match=f"Couldn't clone /srv/proj: couldn't make a directory in {clones}: "
    ):
        await Cloner(clones).clone("/srv/proj", Progress())


async def test_ssh_never_prompts(tmp_path: Path, isolated_git: Path) -> None:
    assert await ssh_command(tmp_path) == "ssh -o BatchMode=yes"


async def test_ssh_keeps_the_command_git_is_configured_with(
    tmp_path: Path, isolated_git: Path
) -> None:
    isolated_git.write_text("[core]\n\tsshCommand = ssh -i ~/.ssh/deploy\n")
    assert await ssh_command(tmp_path) == "ssh -i ~/.ssh/deploy -o BatchMode=yes"


async def test_ssh_keeps_the_command_the_environment_sets(
    tmp_path: Path, isolated_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_git.write_text("[core]\n\tsshCommand = ssh -i ~/.ssh/deploy\n")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -p 2222")
    assert await ssh_command(tmp_path) == "ssh -p 2222 -o BatchMode=yes"


async def test_a_clone_without_credentials_never_prompts_and_hints_at_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "seen"
    script = tmp_path / "ssh.sh"
    script.write_text(
        f'echo "$GIT_TERMINAL_PROMPT $*" > {seen}\n'
        'echo "git@example.invalid: Permission denied (publickey)." >&2\n'
        "exit 255\n"
    )
    monkeypatch.setenv("GIT_SSH_COMMAND", f"sh {script}")
    url = "git@example.invalid:o/proj.git"

    with pytest.raises(CloneFailed) as failed:
        await Cloner(tmp_path / "clones").clone(url, Progress())

    assert seen.read_text().startswith("0 -o BatchMode=yes ")
    assert "Permission denied (publickey)." in str(failed.value)
    assert str(failed.value).endswith(f"\n{AUTH_HINT}")
