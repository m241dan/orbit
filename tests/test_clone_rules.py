"""The rules of cloning a repository to serve it: what may be added and cloned, the directory a
clone goes to, git's progress and what's said when a clone fails."""

import pytest

from orbit.agent.domain.clones import (
    AUTH_HINT,
    added_url,
    clone_failure,
    clone_name,
    clone_names,
    clone_progress,
    looks_like_url,
)
from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID, WorkspaceCloning

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
def test_urls_and_scp_style_addresses_look_like_urls(url: str) -> None:
    assert looks_like_url(url)
    assert added_url(None, url) == url


@pytest.mark.parametrize("text", ["/srv/repos/proj", "proj", "github.com:o/proj", "ftp://x"])
def test_paths_and_other_schemes_dont_look_like_urls(text: str) -> None:
    assert not looks_like_url(text)


def test_an_absolute_path_may_be_cloned() -> None:
    assert added_url(None, "/srv/repos/proj.git") == "/srv/repos/proj.git"


def test_a_path_alone_is_added_by_its_path() -> None:
    assert added_url("/src/orbit", None) is None


@pytest.mark.parametrize("url", ["ftp://x", "proj", "http://github.com/o/proj", "https://"])
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
    ("url", "name"),
    [
        ("https://github.com/o/proj.git", "proj"),
        ("https://github.com/o/proj/", "proj"),
        ("git@github.com:proj.git", "proj"),
        ("file:///srv/repos/my%20proj!.git", "my-20proj"),
        ("/srv/repos/.hidden", "hidden"),
        ("/srv/repos/proj/.git", "repository"),
    ],
)
def test_a_clone_is_named_for_the_urls_last_part_made_safe(url: str, name: str) -> None:
    assert clone_name(url) == name


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
    assert clone_progress(URL, line) == WorkspaceCloning(url=URL, phase=phase, percent=percent)


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
