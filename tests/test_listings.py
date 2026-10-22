"""What frontends browsing the server's host are shown: directories in order, capped, marked,
with their parent, and the paths refused."""

import errno

import pytest

from orbit.agent.domain.listings import (
    MAX_ENTRIES,
    FoundDirectory,
    check_inside,
    check_listable,
    listing,
    unreadable,
)
from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID, UNKNOWN, DirectoryEntry


def found(name: str, *, repository: bool = False, target: str | None = None) -> FoundDirectory:
    return FoundDirectory(name, f"/src/{name}", repository, target)


def test_repositories_come_first_then_the_rest_by_name_ignoring_case_and_dots() -> None:
    directories = [found("l"), found(".hidden"), found("B"), found("b", repository=True)]
    names = [entry.name for entry in listing("/src", "/", directories, {}).entries]
    assert names == ["b", "B", ".hidden", "l"]


def test_entries_are_marked_hidden_served_and_with_their_targets() -> None:
    directories = [found(".cache"), found("orbit", repository=True), found("l", target="a")]
    entries = listing("/src", "/", directories, {"/src/orbit": "w1"}).entries
    assert entries == (
        DirectoryEntry(name="orbit", is_repository=True, workspace="w1", hidden=False),
        DirectoryEntry(name=".cache", is_repository=False, hidden=True),
        DirectoryEntry(name="l", is_repository=False, hidden=False, target="a"),
    )


def test_a_listing_holds_at_most_the_cap_counting_them_all() -> None:
    directories = [found(f"d{index:05}") for index in range(MAX_ENTRIES + 1)]
    shown = listing("/src", "/", directories, {})
    assert (len(shown.entries), shown.truncated, shown.total) == (MAX_ENTRIES, True, 2001)
    assert shown.entries[-1].name == f"d{MAX_ENTRIES - 1:05}"


def test_a_listing_within_the_cap_is_whole() -> None:
    shown = listing("/src", "/", [found("a")], {})
    assert (shown.path, shown.parent, shown.truncated, shown.total) == ("/src", "/", False, 1)


def test_a_relative_path_cant_be_listed() -> None:
    with pytest.raises(Refused) as refused:
        check_listable("src", absolute=False)
    assert (refused.value.code, refused.value.message) == (
        INVALID,
        "Can't list src: give the absolute path to a directory.",
    )


def test_an_absolute_path_can_be_listed() -> None:
    check_listable("/src", absolute=True)


@pytest.mark.parametrize(("root", "inside"), [("/x", True), (None, False)])
def test_a_path_inside_the_root_or_with_none_is_browsable(root: str | None, inside: bool) -> None:
    check_inside("/x/l", root, inside=inside)


def test_a_path_outside_the_root_is_refused() -> None:
    with pytest.raises(Refused) as refused:
        check_inside("/x/l", "/x", inside=False)
    assert (refused.value.code, refused.value.message) == (
        INVALID,
        "/x/l is outside the browsable root /x.",
    )


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            FileNotFoundError(errno.ENOENT, "No such file"),
            UNKNOWN,
            "Can't list /a: there's nothing there.",
        ),
        (PermissionError(errno.EACCES, "Permission denied"), INVALID, "Permission denied: /a"),
        (
            NotADirectoryError(errno.ENOTDIR, "Not a dir"),
            INVALID,
            "Can't list /a: it isn't a directory.",
        ),
        (
            OSError(errno.ELOOP, "Too many levels of symbolic links"),
            INVALID,
            "Can't list /a: Too many levels of symbolic links",
        ),
        (OSError("It broke."), INVALID, "Can't list /a: It broke."),
    ],
)
def test_a_directory_that_cant_be_read_is_refused_saying_why(
    error: OSError, code: str, message: str
) -> None:
    refused = unreadable("/a", error)
    assert (refused.code, refused.message) == (code, message)
