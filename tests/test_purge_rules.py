"""The rules for purging a workspace: what deleting a clone would lose, and refusing then."""

import pytest

from orbit.agent.domain.purging import check_purgeable, clone_losses, unchecked_clone
from orbit.agent.domain.refusals import Refused
from orbit.protocol import INVALID


def test_a_clean_clone_loses_nothing() -> None:
    assert clone_losses(0, 0, [("main", 0)]) == []


def test_each_kind_of_loss_is_a_line_counted_in_words() -> None:
    assert clone_losses(1, 1, [("main", 1)]) == [
        "uncommitted changes to 1 file",
        "1 stash",
        "1 commit on main not on its remote",
    ]
    assert clone_losses(2, 3, [("main", 0), ("fix", 4)]) == [
        "uncommitted changes to 2 files",
        "3 stashes",
        "4 commits on fix not on its remote",
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
