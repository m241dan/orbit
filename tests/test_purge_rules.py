"""The rules for purging a workspace: what deleting a clone would lose, and refusing then or
while another orbit owns it."""

import pytest

from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.purging import (
    check_purgeable,
    clone_losses,
    owned_refusal,
    unchecked_clone,
)
from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID


def test_a_clean_clone_loses_nothing() -> None:
    assert clone_losses(0, 0, [("main", 0)], 0) == []


def test_each_kind_of_loss_is_a_line_counted_in_words() -> None:
    assert clone_losses(1, 1, [("main", 1)], 0) == [
        "uncommitted changes to 1 file",
        "1 stash",
        "1 commit on main not on its remote",
    ]
    assert clone_losses(2, 3, [("main", 0), ("fix", 4)], 0) == [
        "uncommitted changes to 2 files",
        "3 stashes",
        "4 commits on fix not on its remote",
    ]


def test_commits_on_a_detached_head_are_a_line_counted_in_words() -> None:
    assert clone_losses(0, 0, [], 1) == ["1 commit on a detached HEAD not on its remote"]
    assert clone_losses(0, 0, [("main", 2)], 3) == [
        "2 commits on main not on its remote",
        "3 commits on a detached HEAD not on its remote",
    ]


def test_a_clone_that_cant_be_checked_may_lose_anything() -> None:
    assert unchecked_clone("not a git repository") == [
        "whatever it holds, which can't be checked: not a git repository"
    ]


def test_a_clone_losing_nothing_may_be_purged() -> None:
    check_purgeable("/c/proj", [])


def test_a_clone_that_would_lose_work_is_refused_as_invalid_listing_it() -> None:
    with pytest.raises(Refused) as refusal:
        check_purgeable("/c/proj", ["1 stash", "1 commit on main not on its remote"])
    assert refusal.value.code == INVALID
    assert refusal.value.message == (
        "Can't purge /c/proj: deleting its clone would lose 1 stash; 1 commit on main not on its"
        " remote."
    )


def test_a_workspace_another_orbit_owns_is_refused_naming_the_owner() -> None:
    assert owned_refusal("/c/proj", WorkspaceOwner(pid=42)) == (
        "Can't purge /c/proj: an orbit chat, pid 42 owns its repository."
    )
    assert owned_refusal("/c/proj", WorkspaceOwner(pid=42, url="http://host:8000")) == (
        "Can't purge /c/proj: an orbit serve at http://host:8000 owns its repository."
    )


def test_a_workspace_whose_owner_cant_be_read_is_refused_as_another_orbits() -> None:
    assert owned_refusal("/c/proj", WorkspaceOwner()) == (
        "Can't purge /c/proj: another orbit owns its repository."
    )
