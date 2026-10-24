"""The rules of cloning a repository to serve it: what may be added and cloned, the directory a
clone goes to, git's progress and what's said when a clone fails."""

import pytest

from orbit.agent.domain.clones import (
    AUTH_HINT,
    added_url,
    clone_failure,
    clone_names,
    clone_progress,
    clone_served,
    local_clone_source,
)
from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID, WorkspaceCloning, clone_failure_message, clone_name

URL = "https://github.com/o/proj.git"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/proj.git",
        "ssh://git@github.com/o/proj",
        "file:///srv/repos/proj.git",
        "git@github.com:o/proj.git",
    ],
)
def test_urls_and_scp_style_addresses_may_be_cloned(url: str) -> None:
    assert added_url(None, url) == url


def test_an_absolute_path_may_be_cloned() -> None:
    assert added_url(None, "/srv/repos/proj.git") == "/srv/repos/proj.git"


def test_a_path_alone_is_added_by_its_path() -> None:
    assert added_url("/src/orbit", None) is None


@pytest.mark.parametrize(
    "url", ["ftp://x", "proj", "http://github.com/o/proj", "https://", "git@host:", "https://x y"]
)
def test_another_source_is_refused_as_invalid(url: str) -> None:
    with pytest.raises(Refused) as refused:
        added_url(None, url)
    assert refused.value.code == INVALID
    assert str(refused.value).startswith(f"Can't clone {url}: give an https://")


@pytest.mark.parametrize(("path", "url"), [(None, None), ("/src/orbit", URL)])
def test_both_or_neither_of_a_path_and_a_url_is_refused_as_invalid(
    path: str | None, url: str | None
) -> None:
    with pytest.raises(
        Refused, match=r"Give AddWorkspace a path or a URL, one of them\."
    ) as refused:
        added_url(path, url)
    assert refused.value.code == INVALID


@pytest.mark.parametrize(
    "url", [URL, "git@github.com:proj.git", "file:///srv/repos/my%20proj!.git", "/srv/p/.git"]
)
def test_a_clones_progress_and_last_news_name_it_as_the_shared_rule_does(url: str) -> None:
    progress = clone_progress(url, "Receiving objects:  45% (450/1000)")
    assert progress is not None
    assert progress.name == clone_served(url, "w1").name == clone_name(url)


def test_a_name_taken_is_numbered_from_2() -> None:
    names = clone_names("proj")
    assert [next(names) for _ in range(4)] == ["proj", "proj-2", "proj-3", "proj-4"]


@pytest.mark.parametrize(
    ("line", "phase", "percent"),
    [
        ("Receiving objects:  45% (450/1000), 1.2 MiB | 1 MiB/s", "Receiving objects", 45),
        ("remote: Counting objects: 100% (2/2), done.        ", "Counting objects", 100),
        ("remote: Enumerating objects: 2, done.", "Enumerating objects", None),
    ],
)
def test_gits_progress_lines_are_its_phase_and_percent(
    line: str, phase: str, percent: int | None
) -> None:
    expected = WorkspaceCloning(url=URL, name="proj", phase=phase, percent=percent)
    assert clone_progress(URL, line) == expected


def test_a_clone_served_ends_with_the_workspace_it_is_served_as() -> None:
    served = WorkspaceCloning(url=URL, name="proj", phase="Done", percent=100, workspace="w1")
    assert clone_served(URL, "w1") == served


@pytest.mark.parametrize(
    "line",
    [
        "Cloning into 'proj'...",
        "remote: Total 2 (delta 0), reused 0 (delta 0)",
        "fatal: repository 'x' not found",
        "ERROR: Repository not found.",
    ],
)
def test_other_lines_are_no_progress(line: str) -> None:
    assert clone_progress(URL, line) is None


def test_a_failure_gives_gits_last_lines() -> None:
    said = [f"line {n}" for n in range(8)]
    assert clone_failure(URL, said) == f"Couldn't clone {URL}: " + "\n".join(said[-5:])


@pytest.mark.parametrize("said", [[], ["fatal: not found"], ["Permission denied (publickey)."]])
def test_a_failure_is_told_as_the_shared_rule_tells_one(said: list[str]) -> None:
    assert clone_failure(URL, said).startswith(clone_failure_message(URL, ""))


def test_a_failure_git_said_nothing_of_says_so() -> None:
    assert clone_failure(URL, []) == f"Couldn't clone {URL}: git said nothing."


@pytest.mark.parametrize(
    "line",
    [
        "git@github.com: Permission denied (publickey).",
        "fatal: Authentication failed for 'https://github.com/o/proj.git/'",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "Host key verification failed.",
    ],
)
def test_an_authentication_failure_adds_the_credentials_hint(line: str) -> None:
    assert clone_failure(URL, [line]) == f"Couldn't clone {URL}: {line}\n{AUTH_HINT}"


def test_the_hint_names_ssh_keys_and_credential_helpers() -> None:
    assert AUTH_HINT == (
        "The server's account needs access to this repository: an SSH key or a git credential"
        " helper."
    )


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("/srv/repos/proj.git", "/srv/repos/proj.git"),
        ("file:///srv/repos/proj.git", "/srv/repos/proj.git"),
        ("file://localhost/srv/repos/proj.git", "/srv/repos/proj.git"),
        ("file:///srv/my%20repos/proj.git", "/srv/my repos/proj.git"),
    ],
)
def test_an_absolute_path_or_file_url_clones_a_local_path(url: str, path: str) -> None:
    assert local_clone_source(url) == path


@pytest.mark.parametrize(
    "url", ["https://github.com/o/proj.git", "ssh://git@github.com/o/proj", "git@github.com:o/p"]
)
def test_a_remote_url_clones_no_local_path(url: str) -> None:
    assert local_clone_source(url) is None
