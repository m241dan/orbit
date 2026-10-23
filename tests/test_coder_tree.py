"""The coder's tree's rules: how its work is merged, how it catches up, and its leftover notes."""

import pytest

from orbit.agent.domain.coder_tree import (
    CatchUpStep,
    MergeStep,
    catch_up_plan,
    merge_plan,
    stale_snapshots_note,
    stashed_note,
    unmerged_note,
)


@pytest.mark.parametrize("coder_in_base", [False, True])
@pytest.mark.parametrize("base_in_coder", [False, True])
def test_without_a_base_branch_the_work_stays_detached(
    coder_in_base: bool, base_in_coder: bool
) -> None:
    assert merge_plan(False, coder_in_base, base_in_coder) == "detached"


@pytest.mark.parametrize(
    ("coder_in_base", "base_in_coder", "step"),
    [
        (True, False, "merged"),
        (True, True, "merged"),
        (False, True, "fast-forward"),
        (False, False, "rebase"),
    ],
)
def test_with_a_base_branch_the_work_is_merged_as_the_tips_allow(
    coder_in_base: bool, base_in_coder: bool, step: MergeStep
) -> None:
    assert merge_plan(True, coder_in_base, base_in_coder) == step


@pytest.mark.parametrize("base_in_coder", [False, True])
@pytest.mark.parametrize("coder_in_base", [False, True])
def test_a_tree_without_a_clean_base_stays_as_it_is(
    base_in_coder: bool, coder_in_base: bool
) -> None:
    assert catch_up_plan(False, base_in_coder, coder_in_base) == "stay"


@pytest.mark.parametrize(
    ("base_in_coder", "coder_in_base", "step"),
    [
        (True, False, "stay"),
        (True, True, "stay"),
        (False, True, "fast-forward"),
        (False, False, "rebase"),
    ],
)
def test_a_clean_tree_catches_up_as_the_tips_allow(
    base_in_coder: bool, coder_in_base: bool, step: CatchUpStep
) -> None:
    assert catch_up_plan(True, base_in_coder, coder_in_base) == step


def test_no_unmerged_commits_need_no_note() -> None:
    assert unmerged_note(0, "orbit/coder", "main") is None


def test_the_unmerged_note_counts_one_commit_as_one() -> None:
    assert unmerged_note(1, "orbit/coder", "main") == (
        "1 commit on orbit/coder not yet merged into main."
    )


def test_the_unmerged_note_counts_several_commits() -> None:
    assert unmerged_note(3, "orbit/coder", "dev") == (
        "3 commits on orbit/coder not yet merged into dev."
    )


def test_no_stashes_need_no_note() -> None:
    assert stashed_note(0) is None


def test_the_stashed_note_counts_one_stash_as_one() -> None:
    assert stashed_note(1) == "1 stash of unverified orbit work; see git stash list."


def test_the_stashed_note_counts_several_stashes() -> None:
    assert stashed_note(2) == "2 stashes of unverified orbit work; see git stash list."


def test_no_stale_snapshots_need_no_note() -> None:
    assert stale_snapshots_note(0) is None


def test_the_stale_snapshots_note_counts_one_worktree_as_one() -> None:
    assert stale_snapshots_note(1) == (
        "1 snapshot worktree left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    )


def test_the_stale_snapshots_note_counts_several_worktrees() -> None:
    assert stale_snapshots_note(2) == (
        "2 snapshot worktrees left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    )
