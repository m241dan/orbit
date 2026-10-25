"""The rules of the control plane's live gauges, over the plain state the application reads."""

from collections.abc import Collection, Iterable, Mapping


def agents_by_role(
    roles: Mapping[str, Collection[str]], running: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """How many agents run in each role in each workspace served, by the workspace's id and the
    role: ``roles`` gives each workspace served, by its id, with the roles it configures, each
    counted even when none of its agents runs, and ``running`` the workspace and role of each
    agent running, a role the workspace doesn't configure counted too, and an agent of a
    workspace not served not at all."""
    counts = {(workspace, role): 0 for workspace, names in roles.items() for role in names}
    for workspace, role in running:
        if workspace in roles:
            counts[workspace, role] = counts.get((workspace, role), 0) + 1
    return counts
