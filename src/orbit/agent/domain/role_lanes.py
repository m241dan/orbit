"""The rules across roles: how they share lanes and consult one another, and the lanes they make.

A conversational role can't share its lane with a task role; the enabled roles in the worktree share
one lane, as do the enabled review roles (the lens reviewers, per-commit and the super review's, and
the reviewer); and an enabled role may consult only other conversational roles, none round in a
cycle, the disabled ones it names left out. The enabled task roles' lanes are the session's lanes,
the one whose roles merge running on the user's asking; the enabled conversational roles each have a
conversation of their own; and orbit's reviews run in the enabled review roles' lane.
"""

from collections.abc import Mapping

from orbit.agent.domain.roles import Role
from orbit.protocol import (
    LENS_REVIEWERS,
    REVIEW_ROLES,
    REVIEWER,
    SUPER_LENS_REVIEWERS,
    WORK_LANE,
)

_REVIEW_LANE_ORDER = (REVIEWER, *LENS_REVIEWERS, *SUPER_LENS_REVIEWERS)
"""The review roles, in the order :func:`review_lane` looks for an enabled one."""


def lane_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Each conversational role among ``roles`` whose lane a task role shares, naming them: a
    task in that lane would wait on the conversation, or the conversation on it."""
    clashes = []
    for name, role in roles.items():
        shared = _task_roles_in(roles, role.lane) if role.conversation else []
        if shared:
            clashes.append(
                f"the conversational role {name!r} can't share its lane {role.lane!r} with the"
                f" task roles {shared}"
            )
    return clashes


def _task_roles_in(roles: Mapping[str, Role], lane: str) -> list[str]:
    """The roles among ``roles`` in ``lane`` that aren't conversational."""
    return [name for name, role in roles.items() if not role.conversation and role.lane == lane]


def worktree_lane_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Why the enabled roles among ``roles`` that work in the worktree can't, if they're in more
    than one lane, naming them by lane: there's one worktree, and lanes run beside each other."""
    worktree = {
        name: role for name, role in roles.items() if role.enabled and role.workspace == "worktree"
    }
    return _lanes_apart('the enabled roles with workspace = "worktree"', worktree)


def review_lane_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Why the enabled review roles among ``roles`` (the lens reviewers, per-commit and the super
    review's, and the reviewer) can't be where they are, if they're in different lanes, naming
    them by lane: orbit's reviews keep their rules only in the review lane, which runs one lane's
    roles."""
    reviewers = {
        name: role for name, role in roles.items() if role.enabled and name in REVIEW_ROLES
    }
    return _lanes_apart("the enabled review roles", reviewers)


def _lanes_apart(who: str, roles: Mapping[str, Role]) -> list[str]:
    """That ``who``, the ``roles``, must share one lane, if they're in more than one, naming
    them by lane."""
    lanes: dict[str, list[str]] = {}
    for name, role in roles.items():
        lanes.setdefault(role.lane, []).append(name)
    if len(lanes) < 2:
        return []
    named = " and ".join(f"lane {lane!r} has {names}" for lane, names in lanes.items())
    return [f"{who} must share one lane, but {named}"]


def role_clashes(roles: Mapping[str, Role]) -> list[str]:
    """How ``roles`` break the rules across roles: the conversational roles whose lanes task
    roles share (see :func:`lane_clashes`), the worktree's roles in more than one lane (see
    :func:`worktree_lane_clashes`), and the review roles in different lanes (see
    :func:`review_lane_clashes`)."""
    return (
        lane_clashes(roles)
        + worktree_lane_clashes(roles)
        + review_lane_clashes(roles)
        + consult_clashes(roles)
    )


def consult_clashes(roles: Mapping[str, Role]) -> list[str]:
    """Why the enabled roles among ``roles`` can't consult whom they name, naming them: each must
    be another conversational role, and none may consult round in a cycle. A disabled role named
    is left out of the consults (see :func:`with_enabled_consults`), not refused."""
    clashes = [
        f"the role {name!r} can't consult {other!r}, which isn't another conversational role"
        for name, role in roles.items()
        if role.enabled
        for other in role.consults
        if not _consultable(roles, name, other)
    ]
    return clashes + _consult_cycles(with_enabled_consults(roles))


def _consultable(roles: Mapping[str, Role], name: str, other: str) -> bool:
    """Whether the role ``name`` may name ``other`` among ``roles`` in its consults: a disabled
    role is left out of the consults rather than refused, whether or not it converses."""
    if other == name or other not in roles:
        return False
    return roles[other].conversation or not roles[other].enabled


def with_enabled_consults(roles: Mapping[str, Role]) -> dict[str, Role]:
    """The ``roles``, each consulting none of the disabled roles among those it names."""
    return {name: _consulting_enabled(roles, role) for name, role in roles.items()}


def _consulting_enabled(roles: Mapping[str, Role], role: Role) -> Role:
    """The ``role``, consulting none of the disabled roles among ``roles`` it names. A name that
    isn't a role is kept, for :func:`consult_clashes` to refuse."""
    consults = tuple(other for other in role.consults if other not in roles or roles[other].enabled)
    if consults == role.consults:
        return role
    return role.model_copy(update={"consults": consults})


def _consult_cycles(roles: Mapping[str, Role]) -> list[str]:
    """A cycle among the ``roles``' consults, named role by role, if there is one."""
    for start in roles:
        cycle = _cycle_from(roles, [start])
        if cycle:
            return ["a consults cycle: " + " -> ".join(repr(name) for name in cycle)]
    return []


def _onward(roles: Mapping[str, Role], path: list[str], other: str) -> list[str]:
    """The cycle ``path`` carried on through ``other`` makes, unless ``other`` is already on
    it."""
    return [] if other in path else _cycle_from(roles, [*path, other])


def _cycle_from(roles: Mapping[str, Role], path: list[str]) -> list[str]:
    """The ``path`` of consults carried on until it returns to its start, if it can; nothing
    otherwise. A role consulting itself is refused on its own, so isn't a cycle."""
    role = roles.get(path[-1])
    for other in role.consults if role else ():
        if other == path[-1]:
            continue
        cycle = [*path, other] if other == path[0] else _onward(roles, path, other)
        if cycle:
            return cycle
    return []


def task_lanes(roles: Mapping[str, Role]) -> dict[str, frozenset[str]]:
    """The lanes the enabled task roles among ``roles`` run in, in order, each with the names of
    those roles: the roles that aren't conversational."""
    lanes: dict[str, set[str]] = {}
    for name, role in roles.items():
        if role.enabled and not role.conversation:
            lanes.setdefault(role.lane, set()).add(name)
    return {lane: frozenset(names) for lane, names in lanes.items()}


def conversational_roles(roles: Mapping[str, Role]) -> tuple[str, ...]:
    """The names of the enabled conversational roles among ``roles``, in order: those the user
    talks to, each with a conversation of its own."""
    return tuple(name for name, role in roles.items() if role.enabled and role.conversation)


def merging_lane(roles: Mapping[str, Role]) -> str:
    """The lane of the enabled roles among ``roles`` whose work merges, which runs only on the
    user's asking: there's one at most, as only roles in the worktree merge and they share a
    lane. It's the work lane if none merges."""
    return next((role.lane for role in roles.values() if role.enabled and role.merge), WORK_LANE)


def review_lane(roles: Mapping[str, Role]) -> str:
    """The lane that runs orbit's reviews among ``roles``: that of the enabled review roles, as
    they share a lane, the reviewer's first, then each lens reviewer's in turn, per-commit then
    the super review's. It's the reviewer's if they're all disabled, when none is queued."""
    enabled = (
        roles[name].lane for name in _REVIEW_LANE_ORDER if name in roles and roles[name].enabled
    )
    return next(enabled, roles[REVIEWER].lane)
