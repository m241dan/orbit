"""The rules of a clone that the server and its frontends share: what looks like a URL, the name
a clone is given and how a clone's failure is told."""

import pytest

from orbit.protocol import clone_failure_message, clone_name, failed_clone, looks_like_url

URL = "https://github.com/o/proj.git"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/proj.git",
        "ssh://git@github.com/o/proj",
        "file:///srv/repos/proj.git",
        "git@github.com:o/proj.git",
        "https://github.com/o/r",
        "git@github.com:o/r.git",
        "file:///x/r.git",
        "ssh://h/r",
    ],
)
def test_urls_and_scp_style_addresses_look_like_urls(url: str) -> None:
    assert looks_like_url(url)


@pytest.mark.parametrize(
    "text",
    [
        "/srv/repos/proj",
        "proj",
        "github.com:o/proj",
        "ftp://x",
        "/srv/repo",
        "~/repo",
        "/srv/a@b:c",
        "repo:2",
        "git@host:",
        "https://x y",
        "https://",
    ],
)
def test_paths_other_schemes_and_partial_urls_dont_look_like_urls(text: str) -> None:
    assert not looks_like_url(text)


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


def test_a_failure_message_names_the_url_before_the_detail() -> None:
    assert clone_failure_message(URL, "not found") == f"Couldn't clone {URL}: not found"


@pytest.mark.parametrize(
    "detail", ["not found", "git failed to start: no git", "line 1\nline 2", ""]
)
def test_a_failure_message_is_of_the_clone_of_its_url(detail: str) -> None:
    assert failed_clone(clone_failure_message(URL, detail), URL)


def test_a_failure_message_isnt_of_another_urls_clone() -> None:
    message = clone_failure_message("git@host:o/other.git", "denied")
    assert not failed_clone(message, URL)
    assert not failed_clone(clone_failure_message(f"{URL}x", "denied"), URL)


def test_another_message_isnt_a_clones_failure() -> None:
    assert not failed_clone(f"Can't clone {URL}: it's being cloned already.", URL)
