"""Showing the diff of landed commits that a trigger's or a review's task quotes."""

from collections.abc import Sequence

from orbit.agent.application.ports import CoderTree, TreeFailed
from orbit.agent.domain.reviews import word_unshown_diff


async def show_diff(tree: CoderTree, commits: Sequence[str]) -> str:
    """The messages and diffs of ``commits``, as ``tree``'s git shows them, or what says git
    couldn't show them: the tasks quoting them go ahead, as their agents can read the
    commits."""
    try:
        return await tree.show(commits)
    except TreeFailed as exc:  # The task goes ahead; its agent can read the commits.
        return word_unshown_diff(str(exc))
