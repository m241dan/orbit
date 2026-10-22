"""Matching repositories' fingerprints wherever they move, and the notes on re-attaching."""

import pytest

from orbit.agent.domain.fingerprints import (
    fingerprints_match,
    lookalikes_note,
    normalise_origin,
    reattached_note,
)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:o/r.git",
        "git@GitHub.com:o/r",
        "github.com:o/r.git/",
        "ssh://git@github.com/o/r",
        "ssh://git@github.com:22/o/r.git",
        "https://github.com/o/r",
        "https://user@GITHUB.com/o/r.git/",
        "http://github.com/o/r.git",
        "git://github.com/o/r",
    ],
)
def test_every_way_of_naming_a_remote_normalises_to_its_host_and_path(url: str) -> None:
    assert normalise_origin(url) == "github.com/o/r"


def test_a_local_origin_normalises_to_its_path_without_git() -> None:
    assert normalise_origin("/srv/repos/r.git") == "srv/repos/r"
    assert normalise_origin("file:///srv/repos/r.git") == "srv/repos/r"


def test_the_same_roots_match_across_protocols() -> None:
    assert fingerprints_match("b a git@github.com:o/r.git", "a b https://github.com/o/r")


def test_the_same_roots_match_when_either_side_has_no_origin() -> None:
    assert fingerprints_match("a ", "a https://github.com/o/r")
    assert fingerprints_match("a https://github.com/o/r", "a ")
    assert fingerprints_match("a ", "a ")


def test_different_roots_dont_match() -> None:
    assert not fingerprints_match("a https://github.com/o/r", "b https://github.com/o/r")
    assert not fingerprints_match("a ", "a b ")


def test_the_same_roots_with_different_origins_dont_match() -> None:
    assert not fingerprints_match("a https://github.com/o/r", "a https://github.com/fork/r")


def test_a_fingerprint_without_roots_matches_nothing() -> None:
    assert not fingerprints_match("x", "x")


def test_the_notes_name_the_paths() -> None:
    assert (
        reattached_note("/a/repo", "/b/repo")
        == "Re-attached the sessions of /a/repo, which moved to /b/repo."
    )
    assert (
        lookalikes_note("/c/repo", ["/a/repo", "/b/repo"])
        == "/c/repo looks like /a/repo, /b/repo; started it as a new workspace."
    )
