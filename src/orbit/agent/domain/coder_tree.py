"""The coder's tree's rules: how its work is merged into the base branch, how the tree catches
up with that branch, and the notes on what earlier runs left behind. The caller asks git for the
facts and carries out the step."""

from typing import Literal

from orbit.agent.domain.wording import counted, noun

MergeStep = Literal["detached", "merged", "fast-forward", "rebase"]
"""How the coder's work goes into the base branch: not at all, without one; nothing to do, as
the base has it already; the base moved up to it; or replayed onto the base first."""

CatchUpStep = Literal["stay", "fast-forward", "rebase"]
"""How the coder's tree catches up with the base branch: not at all; moved up to it; or its own
work replayed onto it."""


def merge_plan(has_base: bool, coder_in_base: bool, base_in_coder: bool) -> MergeStep:
    """How the coder's work goes into the base branch, given whether there is one
    (``has_base``), whether it has the coder's tip (``coder_in_base``) and whether the coder's
    tip has its own (``base_in_coder``)."""
    if not has_base:
        return "detached"
    if coder_in_base:
        return "merged"
    return "fast-forward" if base_in_coder else "rebase"


def catch_up_plan(ready: bool, base_in_coder: bool, coder_in_base: bool) -> CatchUpStep:
    """How the coder's tree catches up with the base branch, given whether it's ``ready`` to,
    with a base branch and no changes of its own, whether it has the base's tip
    (``base_in_coder``) and whether the base has its own (``coder_in_base``)."""
    if not ready or base_in_coder:
        return "stay"
    return "fast-forward" if coder_in_base else "rebase"


def unmerged_note(count: int, branch: str, base: str) -> str | None:
    """The note that ``count`` commits on ``branch`` aren't merged into ``base``; none for
    none."""
    if not count:
        return None
    return f"{counted(count, 'commit')} on {branch} not yet merged into {base}."


def stashed_note(count: int) -> str | None:
    """The note that ``count`` stashes hold unverified work; none for none."""
    if not count:
        return None
    return f"{counted(count, 'stash', 'stashes')} of unverified orbit work; see git stash list."


def stale_snapshots_note(count: int) -> str | None:
    """The note that earlier runs left ``count`` snapshot worktrees; none for none."""
    if not count:
        return None
    return (
        f"{count} snapshot {noun(count, 'worktree')} left in .git/orbit by earlier runs; "
        "remove them with git worktree remove."
    )
