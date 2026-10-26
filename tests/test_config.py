import tomllib
from typing import cast

import pytest

from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import Setup
from orbit.agent.domain.config import OrbitConfig, load_config, load_layered
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import (
    DEFAULT,
    NO_USER_ROLES,
    PACKAGE,
    Layer,
    PersonalRoleError,
    UserRoles,
    load_user_roles,
)
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.domain.runtimes import CLAUDE, RuntimeRules
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES
from tests.agent_fakes import CLAUDE_MODELS, EVERYTHING, KNOWN_RULES, LIMITED_RUNTIME

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)


def test_defaults_without_a_file() -> None:
    config = load_config({}, BUILT_IN_ROLES)
    assert config == OrbitConfig(roles=BUILT_IN_ROLES)
    assert config.roles == BUILT_IN_ROLES
    assert (config.fix_attempts, config.review_rounds, config.super_review_every) == (5, 3, 10)
    assert (config.review_suggestions, config.super_review_suggestions) == (5, 10)
    assert config.fallback_fixes == 3
    assert config.verification.steps == ()


def test_the_configs_limits_default_to_the_domains() -> None:
    config = OrbitConfig(roles=BUILT_IN_ROLES)
    assert config.limits == Limits()
    assert config.review_limits == ReviewLimits()


def test_a_file_overrides_settings_and_role_fields() -> None:
    config = load_config(
        {
            "fix_attempts": 2,
            "fallback_fixes": 4,
            "roles": {"coder": {"model": "sonnet", "timeout_minutes": 30}},
        },
        BUILT_IN_ROLES,
    )
    assert (config.fix_attempts, config.fallback_fixes) == (2, 4)
    coder = config.roles["coder"]
    assert (coder.model, coder.timeout_minutes) == ("sonnet", 30)
    assert coder.instructions == BUILT_IN_ROLES["coder"].instructions  # Untouched fields kept.
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


def test_the_built_in_roles_are_the_ones_given() -> None:
    reviewer = Role(instructions="Review.", tools=("get_task",))
    assert load_config({}, {"reviewer": reviewer}).roles == {"reviewer": reviewer}


def test_a_file_can_add_a_role() -> None:
    config = load_config(
        {
            "roles": {
                "docs-writer": {
                    "instructions": "Write docs.",
                    "tools": ["get_task", "complete_task"],
                    "verify": False,
                }
            }
        },
        BUILT_IN_ROLES,
    )
    writer = config.roles["docs-writer"]
    assert writer == Role(
        instructions="Write docs.", tools=("get_task", "complete_task"), verify=False
    )
    assert writer.tool_access == "restricted"


def test_a_file_lists_verification_steps_in_order() -> None:
    config = load_config(
        {
            "verification": {
                "steps": [
                    {"name": "Lint", "command": ["ruff", "check"]},
                    {"name": "Tests", "command": ["pytest"]},
                ]
            }
        },
        BUILT_IN_ROLES,
    )
    assert config.verification.steps == (
        VerificationStep(name="Lint", command=("ruff", "check")),
        VerificationStep(name="Tests", command=("pytest",)),
    )


def test_without_a_setup_table_there_is_no_setup() -> None:
    assert load_config({}, BUILT_IN_ROLES).setup is None


def test_setup_watches_nothing_unless_told() -> None:
    config = load_config({"setup": {"command": ["make", "deps"]}}, BUILT_IN_ROLES)
    assert config.setup == Setup(command=("make", "deps"), watch=())


def test_setup_lists_the_files_it_watches() -> None:
    config = load_config(
        {"setup": {"command": ["uv", "sync"], "watch": ["uv.lock", "a/b.txt"]}}, BUILT_IN_ROLES
    )
    assert config.setup == Setup(command=("uv", "sync"), watch=("uv.lock", "a/b.txt"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"fix_attempts": 0}, "fix_attempts"),
        ({"max_checks": 0}, "max_checks"),
        ({"fallback_fixes": 0}, "fallback_fixes"),
        ({"surprise": 1}, "surprise"),
        ({"roles": "everyone"}, "roles"),
        ({"roles": {"coder": {"tools": ["launch_rockets"]}}}, "launch_rockets"),
        ({"roles": {"coder": {"tool_access": "sometimes"}}}, "roles.coder.tool_access"),
        ({"roles": {"new": {"tools": ["get_task"]}}}, "roles.new.instructions"),
        ({"roles": {"coder": {"timeout_minutes": 0}}}, "timeout_minutes"),
        ({"verification": {"steps": [{"name": "", "command": ["x"]}]}}, "step 1 has no name"),
        ({"verification": {"steps": [{"name": "a", "command": []}]}}, "step 1 has no command"),
        ({"verification": {"steps": [{"name": "  ", "command": ["x"]}]}}, "step 1 has no name"),
        ({"verification": {"steps": [{"name": "a", "command": [""]}]}}, "step 1 has no program"),
        (
            {"verification": {"steps": [{"name": "a", "command": ["  ", "x"]}]}},
            "step 1 has no program",
        ),
        ({"verification": {"steps": [{"name": "a"}]}}, "verification.steps.0.command"),
        (
            {
                "verification": {
                    "steps": [
                        {"name": "a", "command": ["x"]},
                        {"name": "b", "command": ["y"]},
                        {"name": "a", "command": ["z"]},
                    ]
                }
            },
            "step 3 has the same name as step 1",
        ),
        ({"verification": {"steps": [{"name": "a", "command": "x", "b": 1}]}}, "steps.0.b"),
        ({"verification": {"surprise": 1}}, "verification.surprise"),
        ({"setup": {}}, "setup.command"),
        ({"setup": {"command": []}}, "setup.command"),
        ({"setup": {"command": ["uv"], "watch": ["/etc/passwd"]}}, "relative"),
        ({"setup": {"command": ["uv"], "watch": ["../uv.lock"]}}, "relative"),
        ({"setup": {"command": ["uv"], "watch": [""]}}, "relative"),
        ({"setup": {"command": ["uv"], "surprise": 1}}, "setup.surprise"),
    ],
)
def test_mistakes_are_reported_readably(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(data, BUILT_IN_ROLES)
    assert message in str(raised.value)
    assert "\n" not in str(raised.value).split(":", 1)[0]


@pytest.mark.parametrize(
    ("roles", "where"),
    [
        ({"coder": {"tools": ["launch_rockets"]}}, "roles.coder.tools"),
        ({"coder": "everything"}, "roles.coder"),
        ({"x": {"instructions": "Do it.", "lane": "b"}}, "roles"),
    ],
)
def test_a_role_problem_and_another_problem_are_refused_together_each_on_its_own_line(
    roles: dict[str, object], where: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"fix_attempts": 0, "roles": roles}, BUILT_IN_ROLES)
    lines = str(raised.value).splitlines()
    assert [line.split(":", 1)[0] for line in lines] == ["fix_attempts", where]


@pytest.mark.parametrize("tools", [["complete_task"], ["propose_task"]])
def test_a_task_role_without_get_task_is_refused(tools: list[str]) -> None:
    roles = {"new": {"instructions": "x", "tools": tools}}
    with pytest.raises(ConfigError, match=r"new.*a task role needs get_task in its tools"):
        load_config({"roles": roles}, BUILT_IN_ROLES)


def test_a_task_role_with_get_task_loads() -> None:
    roles = {"new": {"instructions": "x", "tools": ["get_task"]}}
    config = load_config({"roles": roles}, BUILT_IN_ROLES)
    assert config.roles["new"].tools == ("get_task",)


def custom(**fields: object) -> dict[str, object]:
    """A project file adding the role ``x``, told what to do, with ``fields``."""
    return {"roles": {"x": {"instructions": "Do it.", **fields}}}


def talker(**fields: object) -> dict[str, object]:
    """A project file adding the conversational role ``x`` in its own lane, with ``fields``."""
    conversational: dict[str, object] = {
        "conversation": True,
        "tools": ["propose_task"],
        "verify": False,
        "workspace": "checkout",
        "lane": "talk",
    }
    return custom(**(conversational | fields))


def test_a_custom_role_gets_the_coders_lane_workspace_and_merging() -> None:
    role = load_config(custom(), BUILT_IN_ROLES).roles["x"]
    assert (role.lane, role.workspace) == ("work", "worktree")
    assert (role.merge, role.verify, role.conversation, role.enabled) == (True, True, False, True)


def test_an_unverified_custom_role_still_loads_without_merging() -> None:
    role = load_config(custom(verify=False), BUILT_IN_ROLES).roles["x"]
    assert (role.verify, role.merge) == (False, False)


def test_a_file_overrides_a_built_in_roles_lane_and_enabling() -> None:
    roles = {"reviewer": {"lane": "audit", "enabled": False}}
    reviewer = load_config({"roles": roles}, BUILT_IN_ROLES).roles["reviewer"]
    assert (reviewer.lane, reviewer.enabled) == ("audit", False)
    assert reviewer.workspace == BUILT_IN_ROLES["reviewer"].workspace  # Untouched fields kept.


def test_a_built_in_role_turned_unverified_without_setting_merge_stops_merging() -> None:
    coder = load_config({"roles": {"coder": {"verify": False}}}, BUILT_IN_ROLES).roles["coder"]
    assert (coder.merge, coder.verify) == (False, False)


def test_a_built_in_role_moved_off_the_worktree_without_setting_merge_stops_merging() -> None:
    roles = {"coder": {"workspace": "snapshot", "verify": False}}
    assert load_config({"roles": roles}, BUILT_IN_ROLES).roles["coder"].merge is False


def test_a_verified_built_in_role_moved_off_the_worktree_is_refused_as_it_cant_merge() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"roles": {"coder": {"workspace": "snapshot"}}}, BUILT_IN_ROLES)
    assert str(raised.value) == "roles.coder: verify = true needs merge = true"


def test_a_built_in_role_set_to_merge_unverified_is_refused() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"roles": {"coder": {"verify": False, "merge": True}}}, BUILT_IN_ROLES)
    assert str(raised.value) == "roles.coder: merge = true needs verify = true"


@pytest.mark.parametrize(("name", "merges"), [("coder", True), ("configurator", False)])
def test_a_built_in_role_whose_verify_and_workspace_are_left_alone_keeps_its_merge(
    name: str, merges: bool
) -> None:
    layered = load_layered({"roles": {name: {"model": "sonnet"}}}, BUILT_IN_ROLES)
    assert layered.config.roles[name].merge is merges
    assert layered.role_layers[name]["merge"] == PACKAGE


def test_a_built_in_roles_merge_worked_out_afresh_is_the_defaults() -> None:
    layered = load_layered({"roles": {"coder": {"verify": False}}}, BUILT_IN_ROLES)
    assert layered.role_layers["coder"]["merge"] == DEFAULT
    assert layered.role_layers["coder"]["verify"] == Layer("project")


def test_a_conversational_custom_role_loads_in_a_lane_of_its_own() -> None:
    role = load_config(talker(), BUILT_IN_ROLES).roles["x"]
    assert (role.conversation, role.merge, role.lane) == (True, False, "talk")


def test_conversational_roles_can_share_a_lane() -> None:
    assert load_config(talker(lane="planner"), BUILT_IN_ROLES).roles["x"].lane == "planner"


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (custom(merge=True, workspace="snapshot"), 'merge = true needs workspace = "worktree"'),
        (custom(verify=False, merge=True), "merge = true needs verify = true"),
        (custom(verify=True, merge=False), "verify = true needs merge = true"),
        (
            custom(workspace="checkout", tool_access="full"),
            'workspace = "checkout" can\'t have tool_access = "full"',
        ),
        (
            talker(tools=["get_task", "propose_task"]),
            "conversation = true needs get_task left out of its tools",
        ),
        (talker(merge=True), "conversation = true needs merge = false"),
        (talker(verify=True), "conversation = true needs verify = false"),
        (
            talker(workspace="worktree"),
            'conversation = true needs a workspace other than "worktree"',
        ),
    ],
)
def test_a_role_whose_fields_dont_go_together_is_refused_naming_it_and_the_rule(
    data: dict[str, object], rule: str
) -> None:
    with pytest.raises(ConfigError, match=r"^roles\.x: ") as raised:
        load_config(data, BUILT_IN_ROLES)
    assert rule in str(raised.value)


def test_a_role_breaking_several_rules_is_refused_naming_each() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(talker(verify=True, workspace="worktree", merge=False), BUILT_IN_ROLES)
    assert str(raised.value) == (
        "roles.x: verify = true needs merge = true; conversation = true needs verify = false;"
        ' conversation = true needs a workspace other than "worktree"'
    )


@pytest.mark.parametrize("lane", ["my lane", "", "work/2", "élan"])
def test_a_lane_that_isnt_a_plain_name_is_refused(lane: str) -> None:
    with pytest.raises(ConfigError, match=r"^roles\.x\.lane: .*a lane is a plain name"):
        load_config(custom(lane=lane), BUILT_IN_ROLES)


ROLE_NAME_RULE = "a role's name is a plain name: letters, digits, - and _"


@pytest.mark.parametrize(
    ("name", "key"), [("qa.lead", "roles.qa.lead"), ("qa lead", "roles.qa lead")]
)
def test_a_role_name_that_isnt_a_plain_name_is_refused_naming_its_key(name: str, key: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"roles": {name: {"instructions": "Lead."}}}, BUILT_IN_ROLES)
    assert str(raised.value) == f"{key}: {ROLE_NAME_RULE}"


def test_a_badly_named_role_is_refused_with_its_other_problems() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"roles": {"qa.lead": {"lane": "qa"}}}, BUILT_IN_ROLES)
    lines = str(raised.value).splitlines()
    assert lines == [
        f"roles.qa.lead: {ROLE_NAME_RULE}",
        "roles.qa.lead.instructions: Field required",
    ]


@pytest.mark.parametrize("name", ["qa-lead", "qa_lead", "QA2"])
def test_a_role_with_a_plain_name_loads(name: str) -> None:
    roles = load_config({"roles": {name: {"instructions": "Lead."}}}, BUILT_IN_ROLES).roles
    assert roles[name].instructions == "Lead."


def test_a_conversational_role_sharing_its_lane_with_task_roles_is_refused_naming_them() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(talker(lane="review"), BUILT_IN_ROLES)
    assert str(raised.value) == (
        "roles: the conversational role 'x' can't share its lane 'review' with the task roles"
        " ['reviewer', 'super-reviewer']"
    )


def test_a_task_role_in_the_planners_lane_is_refused_naming_both() -> None:
    reader = custom(lane="planner", workspace="snapshot", verify=False)
    with pytest.raises(ConfigError, match=r"'planner' can't share its lane 'planner'.*\['x'\]$"):
        load_config(reader, BUILT_IN_ROLES)


def test_worktree_roles_in_more_than_one_lane_are_refused_naming_them_and_their_lanes() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(custom(lane="b"), BUILT_IN_ROLES)
    assert str(raised.value) == (
        'roles: the enabled roles with workspace = "worktree" must share one lane, but'
        " lane 'work' has ['coder', 'configurator'] and lane 'b' has ['x']"
    )


@pytest.mark.parametrize(
    "disabled",
    [
        {"x": {"instructions": "Do it.", "lane": "b", "enabled": False}},
        {
            "x": {"instructions": "Do it.", "lane": "b"},
            "coder": {"enabled": False},
            "configurator": {"enabled": False},
        },
    ],
)
def test_a_disabled_worktree_role_can_be_in_a_lane_of_its_own(
    disabled: dict[str, dict[str, object]],
) -> None:
    assert load_config({"roles": disabled}, BUILT_IN_ROLES).roles["x"].lane == "b"


def test_worktree_roles_sharing_a_lane_other_than_work_load() -> None:
    roles = {"x": {"instructions": "Do it.", "lane": "b"}, "coder": {"lane": "b"}}
    roles["configurator"] = {"lane": "b"}
    assert load_config({"roles": roles}, BUILT_IN_ROLES).roles["coder"].lane == "b"


@pytest.mark.parametrize(
    ("roles", "lanes"),
    [
        (
            {"super-reviewer": {"lane": "audit"}},
            "lane 'review' has ['reviewer'] and lane 'audit' has ['super-reviewer']",
        ),
        (
            {"super-reviewer": {"lane": "work"}},
            "lane 'review' has ['reviewer'] and lane 'work' has ['super-reviewer']",
        ),
        (
            {"reviewer": {"lane": "work"}},
            "lane 'work' has ['reviewer'] and lane 'review' has ['super-reviewer']",
        ),
    ],
)
def test_review_roles_in_different_lanes_are_refused_naming_them_and_their_lanes(
    roles: dict[str, dict[str, object]], lanes: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config({"roles": roles}, BUILT_IN_ROLES)
    assert str(raised.value) == (
        f"roles: the enabled reviewer and super-reviewer must share one lane, but {lanes}"
    )


@pytest.mark.parametrize("disabled", ["reviewer", "super-reviewer"])
@pytest.mark.parametrize("lane", ["audit", "work"])
def test_a_disabled_review_role_can_be_in_another_lane_than_the_other(
    disabled: str, lane: str
) -> None:
    roles: dict[str, dict[str, object]] = {"reviewer": {}, "super-reviewer": {"lane": lane}}
    roles[disabled]["enabled"] = False
    config = load_config({"roles": roles}, BUILT_IN_ROLES)
    assert config.roles["super-reviewer"].lane == lane
    assert not config.roles[disabled].enabled


@pytest.mark.parametrize("lane", ["audit", "work"])
def test_review_roles_sharing_a_lane_other_than_review_load(lane: str) -> None:
    roles = {"reviewer": {"lane": lane}, "super-reviewer": {"lane": lane}}
    assert load_config({"roles": roles}, BUILT_IN_ROLES).roles["reviewer"].lane == lane


def test_an_invalid_field_isnt_also_reported_as_the_merge_default_it_stopped() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(custom(verify="sometimes"), BUILT_IN_ROLES)
    assert str(raised.value).startswith("roles.x.verify: ")
    assert "merge" not in str(raised.value)


def test_a_file_sets_a_roles_effort() -> None:
    roles = {"reviewer": {"effort": "high"}}
    assert load_config({"roles": roles}, BUILT_IN_ROLES).roles["reviewer"].effort == "high"


@pytest.mark.parametrize("effort", ["ultracode", "extreme"])
def test_an_effort_claude_doesnt_offer_is_refused_listing_those_it_does(effort: str) -> None:
    with pytest.raises(ConfigError, match=r"^roles\.reviewer\.effort: ") as raised:
        load_config({"roles": {"reviewer": {"effort": effort}}}, BUILT_IN_ROLES)
    assert "'low', 'medium', 'high', 'xhigh' or 'max'" in str(raised.value)


# Personal roles

USER = Layer("user", "/home/me/.config/orbit/config.toml")
HELPER = {
    "instructions": "You help.",
    "lane": "helper",
    "workspace": "snapshot",
    "merge": False,
    "verify": False,
}
"""A personal role's table: a helper in a lane of its own, on a snapshot."""


def personal(**roles: object) -> UserRoles:
    """The user roles of a user config defining ``roles`` whole."""
    return load_user_roles({"roles": roles}, USER, BUILT_IN_ROLES)


def test_a_personal_role_is_a_role_marked_personal() -> None:
    config = load_config({}, BUILT_IN_ROLES, personal(helper=HELPER))
    assert config.roles["helper"] == Role.model_validate(HELPER)
    assert config.personal == {"helper"}
    assert config.notices == ()


def test_a_project_role_replacing_a_personal_one_is_noted() -> None:
    config = load_config(custom(), BUILT_IN_ROLES, personal(x=HELPER))
    assert config.roles["x"].lane == "work"
    assert config.personal == frozenset()
    assert config.notices == ("The project's role x replaces your personal role of that name.",)


def test_a_personal_role_breaking_a_rule_across_the_roles_is_a_personal_role_error() -> None:
    helper = {"instructions": "You help.", "lane": "helper"}
    with pytest.raises(PersonalRoleError, match=r"^roles: the enabled roles"):
        load_config({}, BUILT_IN_ROLES, personal(helper=helper))


def test_the_projects_own_clash_beside_a_personal_role_is_the_projects() -> None:
    helper = {"instructions": "You help.", "lane": "helper"}
    with pytest.raises(ConfigError) as raised:
        load_config(custom(lane="b"), BUILT_IN_ROLES, personal(helper=helper))
    assert not isinstance(raised.value, PersonalRoleError)
    assert "lane 'b' has ['x']" in str(raised.value)


def test_a_project_role_problem_beside_a_personal_role_is_refused_with_the_rest() -> None:
    roles = {"coder": {"tool_access": "sometimes"}}
    with pytest.raises(ConfigError) as raised:
        load_config({"fix_attempts": 0, "roles": roles}, BUILT_IN_ROLES, personal(helper=HELPER))
    assert not isinstance(raised.value, PersonalRoleError)
    lines = str(raised.value).splitlines()
    assert [line.split(":", 1)[0] for line in lines] == ["fix_attempts", "roles.coder.tool_access"]


@pytest.mark.parametrize("key", ["personal", "notices"])
def test_a_project_file_cant_mark_roles_personal_or_add_notices(key: str) -> None:
    with pytest.raises(ConfigError, match=f"^{key}: Extra inputs are not permitted"):
        load_config({key: ["coder"]}, BUILT_IN_ROLES)


# Where each role's fields came from


def test_a_layered_config_is_the_config_with_the_layer_each_role_field_came_from() -> None:
    user = personal(planner={"model": "opus"})
    data = {"fix_attempts": 2, "roles": {"coder": {"model": "sonnet"}}}
    path = "/src/app/.orbit/config.toml"
    layered = load_layered(data, BUILT_IN_ROLES, user, path)
    assert layered.config == load_config(data, BUILT_IN_ROLES, user, path)
    assert layered.role_layers["coder"]["model"] == Layer("project", path)
    assert layered.role_layers["planner"]["model"] == USER
    assert layered.role_layers["coder"]["tools"] == PACKAGE


# A role's runtime

UNKNOWN_RUNTIME = (
    'roles.coder.runtime = "nope": this version of orbit knows the runtimes claude, kiro.'
)


def test_every_role_runs_on_claude_unless_it_says_otherwise() -> None:
    config = load_config(custom(), BUILT_IN_ROLES)
    assert {role.runtime for role in config.roles.values()} == {"claude"}


def test_a_role_on_claude_loads() -> None:
    config = load_config(tomllib.loads('[roles.coder]\nruntime = "claude"'), BUILT_IN_ROLES)
    assert config.roles["coder"].runtime == "claude"


def test_a_role_on_a_runtime_this_version_doesnt_know_is_refused_naming_it() -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(tomllib.loads('[roles.coder]\nruntime = "nope"'), BUILT_IN_ROLES)
    assert str(raised.value) == UNKNOWN_RUNTIME


def test_each_role_on_a_runtime_this_version_doesnt_know_is_named_on_a_line_of_its_own() -> None:
    roles = {"coder": {"runtime": "nope"}, "x": {"instructions": "Do x.", "runtime": "harness"}}
    with pytest.raises(ConfigError) as raised:
        load_config({"roles": roles}, BUILT_IN_ROLES)
    assert str(raised.value).splitlines() == [
        UNKNOWN_RUNTIME,
        'roles.x.runtime = "harness": this version of orbit knows the runtimes claude, kiro.',
    ]


def test_a_layered_config_records_where_each_roles_runtime_came_from() -> None:
    user = load_user_roles({"roles": {"runtime": "claude"}}, USER, BUILT_IN_ROLES)
    data = {"roles": {"coder": {"runtime": "claude"}}}
    layered = load_layered(data, BUILT_IN_ROLES, user, "/src/app/.orbit/config.toml")
    assert layered.role_layers["coder"]["runtime"] == Layer(
        "project", "/src/app/.orbit/config.toml"
    )
    assert layered.role_layers["reviewer"]["runtime"] == USER


# What a role asks of its runtime


def on_lite(name: str, **fields: object) -> dict[str, object]:
    """A project file putting role ``name`` on :data:`LIMITED_RUNTIME`, with ``fields``."""
    return {"roles": {name: {"runtime": LIMITED_RUNTIME, **fields}}}


def refusal(data: dict[str, object], user: UserRoles = NO_USER_ROLES) -> str:
    """Why the project file ``data`` is refused over the roles ``user`` sets, its roles held to
    what :data:`KNOWN_RULES` say their runtimes can do."""
    with pytest.raises(ConfigError) as raised:
        load_config(data, BUILT_IN_ROLES, user, rules=KNOWN_RULES)
    return str(raised.value)


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_for_effort_its_runtime_takes_none_of_is_refused() -> None:
    assert refusal(on_lite("reviewer", effort="max")) == (
        'roles.reviewer asks for effort "max", but its runtime lite accepts no effort setting.'
    )


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_for_tool_access_its_runtime_doesnt_take_is_refused_listing_it() -> None:
    assert refusal(on_lite("coder")) == (
        'roles.coder asks for tool_access "full", but its runtime lite accepts only tool_access'
        ' "restricted".'
    )


@pytest.mark.usefixtures("limited_runtime")
def test_a_conversational_role_on_a_runtime_that_cant_resume_is_refused() -> None:
    assert refusal(on_lite("planner")) == (
        "roles.planner asks for conversation = true, but its runtime lite doesn't support resume."
    )


@pytest.mark.usefixtures("limited_runtime")
def test_the_user_configs_effort_for_every_role_is_held_to_each_roles_runtime() -> None:
    every = load_user_roles({"roles": {"effort": "low"}}, USER, BUILT_IN_ROLES)
    assert refusal(on_lite("reviewer"), every) == (
        'roles.reviewer asks for effort "low", but its runtime lite accepts no effort setting.'
    )


@pytest.mark.usefixtures("limited_runtime")
def test_each_role_asking_beyond_its_runtime_is_named_on_a_line_of_its_own() -> None:
    data: dict[str, object] = {
        "roles": {"planner": {"runtime": LIMITED_RUNTIME}, "coder": {"runtime": LIMITED_RUNTIME}}
    }
    assert [line.split(" asks")[0] for line in refusal(data).splitlines()] == [
        "roles.planner",
        "roles.coder",
    ]


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_on_a_runtime_without_usage_loads() -> None:
    config = load_config(on_lite("reviewer"), BUILT_IN_ROLES, rules=KNOWN_RULES)
    assert config.roles["reviewer"].runtime == LIMITED_RUNTIME


@pytest.mark.usefixtures("limited_runtime")
def test_a_disabled_role_asking_beyond_its_runtime_loads() -> None:
    data = on_lite("coder", effort="max", enabled=False)
    config = load_config(data, BUILT_IN_ROLES, rules=KNOWN_RULES)
    assert not config.roles["coder"].enabled


def test_on_claude_every_valid_configuration_loads_as_it_did() -> None:
    data = {
        "roles": {
            "planner": {"effort": "low", "tool_access": "off"},
            "coder": {"effort": "max"},
            "reviewer": {"effort": "xhigh", "tool_access": "full", "workspace": "snapshot"},
        }
    }
    claude = RuntimeRules({CLAUDE: EVERYTHING}, {CLAUDE: CLAUDE_MODELS})
    assert (
        load_config(data, BUILT_IN_ROLES, rules=claude).roles
        == load_config(data, BUILT_IN_ROLES).roles
    )
    assert load_config({}, BUILT_IN_ROLES, rules=claude).roles == BUILT_IN_ROLES


# The models a role's runtime takes

CLAUDES_ALIASES = (
    "the runtime claude takes the aliases fable, opus, sonnet, haiku or a full id starting with"
    ' "claude-"'
)
"""What says which models Claude Code takes, as orbit ships its aliases."""


def test_a_roles_model_its_runtime_doesnt_take_is_refused_listing_the_aliases() -> None:
    data: dict[str, object] = {"roles": {"reviewer": {"model": "sonet"}}}
    assert refusal(data) == f'roles.reviewer.model = "sonet": {CLAUDES_ALIASES}.'


def test_a_roles_full_model_id_loads_as_it_is() -> None:
    data = {"roles": {"reviewer": {"model": "claude-sonnet-5-5"}}}
    config = load_config(data, BUILT_IN_ROLES, rules=KNOWN_RULES)
    assert config.roles["reviewer"].model == "claude-sonnet-5-5"


def test_the_user_configs_model_for_every_role_is_refused_once_as_written() -> None:
    every = load_user_roles({"roles": {"model": "sonet"}}, USER, BUILT_IN_ROLES)
    assert refusal({"roles": {"coder": {"model": "opus"}}}, every).splitlines() == [
        f'roles.model = "sonet": {CLAUDES_ALIASES}.'
    ]


def test_a_config_has_the_models_each_runtime_takes() -> None:
    assert load_config({}, BUILT_IN_ROLES, rules=KNOWN_RULES).models == {CLAUDE: CLAUDE_MODELS}
    assert load_config({}, BUILT_IN_ROLES).models == {}


# Settings of a later version of orbit

LATER_VERSION = (
    "isn't supported by this version of orbit yet; it's in the reference config for a later"
    " version. Leave it commented out."
)


@pytest.mark.parametrize(
    ("toml", "setting"),
    [
        ('[triggers.review-each-commit]\non = "task_landed"', "[triggers.review-each-commit]"),
        ('[roles.coder.tools]\nmcp = ["semgrep"]', "[roles.coder.tools]"),
        ('[mcp.github]\ncommand = ["github-mcp-server", "stdio"]', "[mcp.github]"),
        ('[models.local]\nmodel = "openai:qwen3-coder"', "[models.local]"),
        ("[triggers]", "[triggers]"),
        ("[mcp]\nshared = true\n\n[mcp.github]\nshared = false", "[mcp.github]"),
        ('models = "local"', "[models]"),
    ],
)
def test_a_later_versions_setting_is_refused_naming_it(toml: str, setting: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(tomllib.loads(toml), BUILT_IN_ROLES)
    assert str(raised.value) == f"{setting} {LATER_VERSION}"


def test_the_first_later_versions_setting_in_the_file_is_the_one_named() -> None:
    toml = '[roles.coder.tools]\nmcp = []\n\n[mcp.github]\nurl = "http://127.0.0.1:3010/mcp"'
    with pytest.raises(ConfigError, match=r"^\[roles\.coder\.tools\] isn't supported"):
        load_config(tomllib.loads(toml), BUILT_IN_ROLES)


def test_a_layered_config_refuses_a_later_versions_setting_too() -> None:
    with pytest.raises(ConfigError, match=r"^\[mcp\.github\] isn't supported"):
        load_layered({"mcp": {"github": {"shared": True}}}, BUILT_IN_ROLES)


def test_a_roles_list_of_orbit_tools_still_loads() -> None:
    tools = ("get_task", "complete_task", "report_progress", "give_up")
    toml = f"[roles.coder]\ntools = {list(tools)}"
    assert load_config(tomllib.loads(toml), BUILT_IN_ROLES).roles["coder"].tools == tools


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"roles": {"coder": {"colour": "red"}}}, "roles.coder.colour: Extra inputs"),
        ({"roles": {"coder": "harness"}}, "roles.coder: Input should be a valid dictionary"),
        ({"colour": "red"}, "colour: Extra inputs are not permitted"),
        ({"runtime": "claude"}, "runtime: Extra inputs are not permitted"),
    ],
)
def test_other_unknown_settings_keep_their_errors(data: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(data, BUILT_IN_ROLES)
    assert str(raised.value).startswith(message)
    assert "later version" not in str(raised.value)


def test_a_conversational_role_without_propose_task_or_get_task_loads() -> None:
    role = load_config(talker(tools=["report_progress"]), BUILT_IN_ROLES).roles["x"]
    assert role.tools == ("report_progress",)


def test_a_conversational_role_with_only_get_task_is_refused() -> None:
    with pytest.raises(ConfigError, match="get_task left out"):
        load_config(talker(tools=["get_task"]), BUILT_IN_ROLES)


def test_consultation_fields_default_to_none_six_and_a_day() -> None:
    role = load_config(custom(), BUILT_IN_ROLES).roles["x"]
    assert (role.consults, role.consult_limit_per_hour, role.copy_ttl_hours) == ((), 6, 24)


@pytest.mark.parametrize("data", [custom(consult_limit_per_hour=-1), custom(copy_ttl_hours=0)])
def test_a_negative_consult_limit_or_a_zero_copy_ttl_is_refused(data: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match=r"^roles\.x\."):
        load_config(data, BUILT_IN_ROLES)


def test_a_conversational_role_may_consult_the_planner() -> None:
    role = load_config(talker(consults=["planner"]), BUILT_IN_ROLES).roles["x"]
    assert role.consults == ("planner",)


def test_consults_on_a_task_role_is_refused() -> None:
    roles = {"coder": {"consults": ["planner"]}}
    with pytest.raises(ConfigError, match=r"^roles\.coder: consults needs conversation = true$"):
        load_config({"roles": roles}, BUILT_IN_ROLES)


@pytest.mark.parametrize("target", ["coder", "x", "nobody"])
def test_consulting_anything_but_another_enabled_conversational_role_is_refused(
    target: str,
) -> None:
    with pytest.raises(ConfigError, match=f"'x'.*'{target}'"):
        load_config(talker(consults=[target]), BUILT_IN_ROLES)


def with_planner(data: dict[str, object], **fields: object) -> dict[str, object]:
    """The project file ``data``, a role ``x``, also setting the planner's ``fields``."""
    roles = cast(dict[str, object], data["roles"])
    return {"roles": {**roles, "planner": fields}}


def test_consulting_a_disabled_conversational_role_is_refused() -> None:
    data = with_planner(talker(consults=["planner"]), enabled=False)
    with pytest.raises(ConfigError, match="'planner'"):
        load_config(data, BUILT_IN_ROLES)


def test_roles_consulting_each_other_are_refused_naming_the_cycle() -> None:
    data = with_planner(talker(consults=["planner"]), consults=["x"])
    with pytest.raises(ConfigError, match=r"consults cycle.*'planner' -> 'x' -> 'planner'"):
        load_config(data, BUILT_IN_ROLES)
