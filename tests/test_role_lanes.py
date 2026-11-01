from typing import Any

import pytest

from orbit.agent.domain.role_lanes import (
    consult_clashes,
    conversational_roles,
    lane_clashes,
    merging_lane,
    review_lane,
    review_lane_clashes,
    role_clashes,
    task_lanes,
    with_enabled_consults,
    worktree_lane_clashes,
)
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES, LENS_REVIEWERS, WORK_LANE

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)
LENSES = list(LENS_REVIEWERS)
"""The lens reviewers' names, in order, as a lane's roles are listed."""


def with_roles(updates: dict[str, dict[str, Any]]) -> dict[str, Role]:
    """The built-in roles, each named in ``updates`` with the fields it gives changed."""
    changed = {
        name: BUILT_IN_ROLES[name].model_copy(update=fields) for name, fields in updates.items()
    }
    return {**BUILT_IN_ROLES, **changed}


def test_the_built_in_roles_break_no_rule_across_roles() -> None:
    assert role_clashes(BUILT_IN_ROLES) == []


def test_a_conversational_role_sharing_a_task_roles_lane_is_a_clash_naming_them() -> None:
    assert lane_clashes(with_roles({"planner": {"lane": "review"}})) == [
        "the conversational role 'planner' can't share its lane 'review' with the task roles"
        f" {['reviewer', 'super-reviewer', *LENSES]}"
    ]


def test_conversational_roles_sharing_a_lane_are_no_clash() -> None:
    assert lane_clashes(with_roles({"experimenter": {"lane": "planner"}})) == []


def test_enabled_worktree_roles_in_two_lanes_are_a_clash_naming_them_by_lane() -> None:
    assert worktree_lane_clashes(with_roles({"configurator": {"lane": "setup"}})) == [
        'the enabled roles with workspace = "worktree" must share one lane, but'
        " lane 'work' has ['coder'] and lane 'setup' has ['configurator']"
    ]


def test_a_disabled_worktree_role_in_a_lane_of_its_own_is_no_clash() -> None:
    roles = with_roles({"configurator": {"lane": "setup", "enabled": False}})
    assert worktree_lane_clashes(roles) == []


def test_the_enabled_review_roles_apart_are_a_clash_naming_them_by_lane() -> None:
    assert review_lane_clashes(with_roles({"super-reviewer": {"lane": "audit"}})) == [
        "the enabled review roles must share one lane, but"
        f" lane 'review' has {['reviewer', *LENSES]} and lane 'audit' has ['super-reviewer']"
    ]


def test_a_lens_reviewer_apart_from_the_other_review_roles_is_a_clash() -> None:
    assert review_lane_clashes(with_roles({"tests-reviewer": {"lane": "audit"}})) == [
        "the enabled review roles must share one lane, but lane 'review' has"
        f" {['reviewer', 'super-reviewer', *(n for n in LENSES if n != 'tests-reviewer')]}"
        " and lane 'audit' has ['tests-reviewer']"
    ]


def test_a_disabled_review_role_apart_from_the_others_is_no_clash() -> None:
    roles = with_roles({"reviewer": {"enabled": False, "lane": "audit"}})
    assert review_lane_clashes(roles) == []
    lens = with_roles({"bugs-reviewer": {"enabled": False, "lane": "audit"}})
    assert review_lane_clashes(lens) == []


def test_the_clashes_across_roles_come_lane_worktree_review_then_consult() -> None:
    roles = with_roles(
        {
            "planner": {"lane": "review", "consults": ("coder",)},
            "configurator": {"lane": "setup"},
            "super-reviewer": {"lane": "audit"},
        }
    )
    assert role_clashes(roles) == [
        "the conversational role 'planner' can't share its lane 'review' with the task roles"
        f" {['reviewer', *LENSES]}",
        'the enabled roles with workspace = "worktree" must share one lane, but'
        " lane 'work' has ['coder'] and lane 'setup' has ['configurator']",
        "the enabled review roles must share one lane, but"
        f" lane 'review' has {['reviewer', *LENSES]} and lane 'audit' has ['super-reviewer']",
        "the role 'planner' can't consult 'coder', which isn't another conversational role",
    ]


def test_the_built_in_task_roles_run_in_the_work_and_review_lanes() -> None:
    assert task_lanes(BUILT_IN_ROLES) == {
        "work": frozenset({"coder", "configurator"}),
        "review": frozenset({"reviewer", "super-reviewer", *LENSES}),
    }


def test_a_task_role_in_a_lane_of_its_own_adds_that_lane() -> None:
    security = Role(instructions="Audit.", verify=False, lane="security", workspace="snapshot")
    lanes = task_lanes({**BUILT_IN_ROLES, "security": security})
    assert lanes["security"] == frozenset({"security"})


def test_a_disabled_role_adds_no_lane() -> None:
    security = Role(
        instructions="Audit.", verify=False, lane="security", workspace="snapshot", enabled=False
    )
    assert "security" not in task_lanes({**BUILT_IN_ROLES, "security": security})


def test_the_built_in_conversational_roles_are_the_planner_and_experimenter() -> None:
    assert conversational_roles(BUILT_IN_ROLES) == ("planner", "experimenter")


def test_the_conversational_roles_are_the_enabled_ones_in_conversation_in_order() -> None:
    architect = BUILT_IN_ROLES["planner"].model_copy(update={"lane": "design"})
    muted = architect.model_copy(update={"lane": "muted", "enabled": False})
    roles = {"architect": architect, **BUILT_IN_ROLES, "muted": muted}
    assert conversational_roles(roles) == ("architect", "planner", "experimenter")


def test_the_merging_lane_is_the_lane_of_the_roles_that_merge() -> None:
    coder = BUILT_IN_ROLES["coder"].model_copy(update={"lane": "build"})
    configurator = BUILT_IN_ROLES["configurator"].model_copy(update={"lane": "build"})
    roles = {**BUILT_IN_ROLES, "coder": coder, "configurator": configurator}
    assert merging_lane(BUILT_IN_ROLES) == WORK_LANE
    assert merging_lane(roles) == "build"


def test_without_an_enabled_merging_role_the_merging_lane_is_the_work_lane() -> None:
    coder = BUILT_IN_ROLES["coder"].model_copy(update={"lane": "build", "enabled": False})
    assert merging_lane({**BUILT_IN_ROLES, "coder": coder}) == WORK_LANE


def with_review_roles(reviewer: dict[str, Any], super_reviewer: dict[str, Any]) -> dict[str, Role]:
    """The built-in roles, with the ``reviewer`` and ``super_reviewer`` fields changed."""
    return {
        **BUILT_IN_ROLES,
        "reviewer": BUILT_IN_ROLES["reviewer"].model_copy(update=reviewer),
        "super-reviewer": BUILT_IN_ROLES["super-reviewer"].model_copy(update=super_reviewer),
    }


@pytest.mark.parametrize(
    ("reviewer", "super_reviewer", "lane"),
    [
        ({}, {}, "review"),
        ({}, {"lane": "audit"}, "review"),
        ({"enabled": False}, {"lane": "audit"}, "audit"),
        ({"enabled": False}, {"lane": "audit", "enabled": False}, "review"),
    ],
)
def test_the_review_lane_is_the_enabled_reviewers_else_the_super_reviewers(
    reviewer: dict[str, Any], super_reviewer: dict[str, Any], lane: str
) -> None:
    assert review_lane(with_review_roles(reviewer, super_reviewer)) == lane


def with_lenses(**fields: Any) -> dict[str, Role]:
    """The built-in roles, the reviewer and super-reviewer disabled and the lens reviewers'
    ``fields`` changed."""
    lenses = {name: BUILT_IN_ROLES[name].model_copy(update=fields) for name in LENSES}
    return {**with_review_roles({"enabled": False}, {"enabled": False}), **lenses}


def test_with_the_reviewer_and_super_reviewer_disabled_the_review_lane_is_the_lenses() -> None:
    assert review_lane(with_lenses(lane="lenses")) == "lenses"


def test_with_every_review_role_disabled_the_review_lane_is_the_reviewers() -> None:
    assert review_lane(with_lenses(lane="lenses", enabled=False)) == "review"


def consulting_roles(target: str, **coder: object) -> dict[str, Role]:
    """The built-in roles, the planner consulting ``target`` and the coder updated by ``coder``."""
    planner = BUILT_IN_ROLES["planner"].model_copy(update={"consults": (target,)})
    updated = BUILT_IN_ROLES["coder"].model_copy(update=coder)
    return {**BUILT_IN_ROLES, "planner": planner, "coder": updated}


def test_consulting_a_disabled_task_role_is_no_clash() -> None:
    assert consult_clashes(consulting_roles("coder", enabled=False)) == []


@pytest.mark.parametrize("target", ["coder", "nobody"])
def test_consulting_an_enabled_task_role_or_no_role_is_a_clash(target: str) -> None:
    assert consult_clashes(consulting_roles(target)) == [
        f"the role 'planner' can't consult {target!r}, which isn't another conversational role"
    ]


def test_a_consults_cycle_is_a_clash_naming_it_role_by_role() -> None:
    roles = with_roles({"experimenter": {"consults": ("planner",)}})
    assert consult_clashes(roles) == ["a consults cycle: 'planner' -> 'experimenter' -> 'planner'"]


def test_a_role_consulting_itself_is_refused_on_its_own_not_as_a_cycle() -> None:
    assert consult_clashes(with_roles({"planner": {"consults": ("planner",)}})) == [
        "the role 'planner' can't consult 'planner', which isn't another conversational role"
    ]


def test_a_cycle_through_a_disabled_role_is_no_clash() -> None:
    roles = with_roles({"experimenter": {"consults": ("planner",), "enabled": False}})
    assert consult_clashes(roles) == []


def test_a_disabled_role_is_dropped_from_the_consults_and_the_others_kept() -> None:
    roles = with_roles(
        {"planner": {"consults": ("experimenter", "nobody")}, "experimenter": {"enabled": False}}
    )
    assert with_enabled_consults(roles)["planner"].consults == ("nobody",)


def test_roles_consulting_only_enabled_roles_keep_their_consults() -> None:
    assert with_enabled_consults(BUILT_IN_ROLES) == BUILT_IN_ROLES
