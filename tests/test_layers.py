"""The layers orbit's configuration is resolved from, and the roles resolved across them."""

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import (
    DEFAULT,
    NO_USER_ROLES,
    PACKAGE,
    Layer,
    PersonalRoleError,
    ProjectRoles,
    RoleFields,
    UserRoles,
    check_personal,
    load_user_roles,
    merge_roles,
    replaced_note,
    resolve_roles,
)
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES, PLANNER

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
PACKAGE_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)
USER = Layer("user", "/home/me/.config/orbit/config.toml")
PROJECT = Layer("project", "/src/app/.orbit/config.toml")
NO_PROJECT_ROLES = ProjectRoles(PROJECT)
HELPER = {
    "instructions": "You help.",
    "lane": "helper",
    "workspace": "snapshot",
    "merge": False,
    "verify": False,
}
"""A personal role's table: a helper in a lane of its own, on a snapshot."""


def user(data: dict[str, object]) -> UserRoles:
    """The user roles a user config holding ``data`` sets, beside the package's roles."""
    return load_user_roles(data, USER, PACKAGE_ROLES)


def project(roles: dict[str, object]) -> ProjectRoles:
    """The roles of a project file whose ``[roles]`` is ``roles``."""
    return ProjectRoles(PROJECT, roles)


# Layers


def test_a_layer_reads_as_its_name_then_its_file() -> None:
    assert (str(DEFAULT), str(PACKAGE)) == ("default", "package")
    assert str(USER) == "user /home/me/.config/orbit/config.toml"
    assert str(PROJECT) == "project /src/app/.orbit/config.toml"


# The user config's roles


def test_a_user_config_without_roles_sets_none_and_its_other_tables_are_ignored() -> None:
    assert user({"storage": {"tool_output_kb": 1}, "server": {"port": 1}}) == UserRoles(USER)


def test_the_user_configs_roles_table_sets_model_and_effort_for_every_role() -> None:
    roles = user({"roles": {"model": "haiku", "effort": "low"}})
    assert roles.every == RoleFields(model="haiku", effort="low")
    assert roles.named == {}


def test_a_user_configs_role_table_sets_that_roles_model_and_effort() -> None:
    roles = user({"roles": {"model": "haiku", "planner": {"model": "opus", "effort": "max"}}})
    assert roles.named == {PLANNER: RoleFields(model="opus", effort="max")}
    assert roles.every == RoleFields(model="haiku")


@pytest.mark.parametrize(
    ("roles", "key"),
    [({"coder": {"verify": False}}, "roles.coder.verify"), ({"verify": False}, "roles.verify")],
)
def test_any_other_role_field_belongs_in_the_project_file(
    roles: dict[str, object], key: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        user({"roles": roles})
    assert str(raised.value) == (
        f"{key}: belongs in the project's .orbit/config.toml; the user config sets only a"
        " role's model and effort"
    )


@pytest.mark.parametrize(
    ("roles", "key"),
    [
        ({"effort": "extreme"}, "roles.effort"),
        ({"coder": {"effort": "ultracode"}}, "roles.coder.effort"),
    ],
)
def test_an_effort_claude_doesnt_offer_is_refused_listing_those_it_does(
    roles: dict[str, object], key: str
) -> None:
    with pytest.raises(ConfigError, match=f"^{key}: ") as raised:
        user({"roles": roles})
    assert "'low', 'medium', 'high', 'xhigh' or 'max'" in str(raised.value)


def test_a_roles_value_that_isnt_a_table_is_refused() -> None:
    with pytest.raises(ConfigError, match=r"^roles: "):
        user({"roles": "everyone"})


def test_a_table_setting_more_than_model_and_effort_for_a_new_role_is_a_personal_role() -> None:
    roles = user({"roles": {"model": "haiku", "helper": HELPER}})
    assert roles.personal == {"helper": Role.model_validate(HELPER)}
    assert (roles.named, roles.every) == ({}, RoleFields(model="haiku"))


def test_a_table_setting_only_model_and_effort_for_a_new_role_isnt_a_personal_role() -> None:
    roles = user({"roles": {"docs-writer": {"model": "opus", "effort": "low"}}})
    assert roles.named == {"docs-writer": RoleFields(model="opus", effort="low")}
    assert roles.personal == {}


@pytest.mark.parametrize(
    ("helper", "problem"),
    [
        ({"lane": "helper"}, "roles.helper.instructions: Field required"),
        ({**HELPER, "colour": "red"}, "roles.helper.colour: Extra inputs are not permitted"),
        (
            {"instructions": "You help.", "workspace": "snapshot"},
            "roles.helper: verify = true needs merge = true",
        ),
        (
            {"instructions": "You help.", "verify": "sometimes"},
            "roles.helper.verify: Input should be a valid boolean",
        ),
    ],
)
def test_a_personal_role_is_held_to_a_roles_rules_naming_the_key(
    helper: dict[str, object], problem: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        user({"roles": {"helper": helper}})
    assert str(raised.value).startswith(problem)
    assert len(str(raised.value).splitlines()) == 1


def test_every_problem_with_the_user_configs_roles_is_named_on_a_line_of_its_own() -> None:
    with pytest.raises(ConfigError) as raised:
        user({"roles": {"model": 3, "coder": {"lane": "b"}}})
    lines = str(raised.value).splitlines()
    assert [line.split(":", 1)[0] for line in lines] == ["roles.model", "roles.coder.lane"]


# Resolving the roles


def test_without_user_or_project_roles_the_packages_roles_are_the_roles() -> None:
    assert resolve_roles(PACKAGE_ROLES, NO_USER_ROLES, NO_PROJECT_ROLES).roles == PACKAGE_ROLES


def test_a_field_the_package_sets_comes_from_it_and_any_other_from_the_defaults() -> None:
    layers = resolve_roles(PACKAGE_ROLES, NO_USER_ROLES, NO_PROJECT_ROLES).layers
    assert set(layers) == set(PACKAGE_ROLES)
    assert set(layers["coder"]) == set(Role.model_fields)
    assert layers["super-reviewer"]["model"] == PACKAGE
    assert (layers["coder"]["model"], layers["coder"]["effort"]) == (DEFAULT, DEFAULT)
    assert layers["coder"]["tools"] == PACKAGE


def test_the_user_configs_role_table_sets_that_roles_model() -> None:
    resolved = resolve_roles(
        PACKAGE_ROLES, user({"roles": {PLANNER: {"model": "opus"}}}), NO_PROJECT_ROLES
    )
    assert resolved.roles[PLANNER].model == "opus"
    assert resolved.layers[PLANNER]["model"] == USER
    assert resolved.roles["coder"].model is None


def test_the_projects_role_value_wins_over_the_users() -> None:
    resolved = resolve_roles(
        PACKAGE_ROLES,
        user({"roles": {PLANNER: {"model": "opus", "effort": "high"}}}),
        project({PLANNER: {"model": "sonnet"}}),
    )
    planner = resolved.roles[PLANNER]
    assert (planner.model, resolved.layers[PLANNER]["model"]) == ("sonnet", PROJECT)
    assert (planner.effort, resolved.layers[PLANNER]["effort"]) == ("high", USER)


def test_the_users_value_for_every_role_wins_over_the_packages() -> None:
    resolved = resolve_roles(PACKAGE_ROLES, user({"roles": {"model": "haiku"}}), NO_PROJECT_ROLES)
    assert {role.model for role in resolved.roles.values()} == {"haiku"}
    assert {layers["model"] for layers in resolved.layers.values()} == {USER}


def test_the_users_value_for_one_role_wins_over_the_users_value_for_every_role() -> None:
    roles = {"model": "haiku", "effort": "low", "super-reviewer": {"model": "opus"}}
    resolved = resolve_roles(PACKAGE_ROLES, user({"roles": roles}), NO_PROJECT_ROLES)
    reviewer = resolved.roles["super-reviewer"]
    assert (reviewer.model, reviewer.effort) == ("opus", "low")
    assert resolved.roles["reviewer"].model == "haiku"


def test_the_projects_role_value_wins_over_the_users_value_for_every_role() -> None:
    resolved = resolve_roles(
        PACKAGE_ROLES,
        user({"roles": {"model": "haiku"}}),
        project({"super-reviewer": {"model": "opus"}}),
    )
    assert resolved.roles["super-reviewer"].model == "opus"
    assert resolved.layers["super-reviewer"]["model"] == PROJECT
    assert resolved.roles["coder"].model == "haiku"


def test_a_role_the_project_adds_takes_the_users_fields_after_the_packages_roles() -> None:
    writer = {"instructions": "Write docs.", "verify": False}
    resolved = resolve_roles(
        PACKAGE_ROLES, user({"roles": {"effort": "max"}}), project({"docs-writer": writer})
    )
    assert list(resolved.roles) == [*PACKAGE_ROLES, "docs-writer"]
    assert resolved.roles["docs-writer"] == Role(
        instructions="Write docs.", verify=False, effort="max"
    )
    layers = resolved.layers["docs-writer"]
    assert (layers["instructions"], layers["effort"], layers["tools"]) == (PROJECT, USER, DEFAULT)


def test_a_personal_role_comes_after_the_packages_roles_and_before_the_projects() -> None:
    writer = {"instructions": "Write docs.", "verify": False}
    resolved = resolve_roles(
        PACKAGE_ROLES, user({"roles": {"helper": HELPER}}), project({"docs-writer": writer})
    )
    assert list(resolved.roles) == [*PACKAGE_ROLES, "helper", "docs-writer"]
    assert resolved.roles["helper"] == Role.model_validate(HELPER)
    layers = resolved.layers["helper"]
    assert (layers["instructions"], layers["lane"], layers["tools"]) == (USER, USER, DEFAULT)
    assert resolved.personal == {"helper"}
    assert resolved.replaced == ()


def test_a_personal_role_takes_the_users_fields_for_every_role_beneath_its_own() -> None:
    roles = {"model": "haiku", "effort": "low", "helper": {**HELPER, "effort": "max"}}
    resolved = resolve_roles(PACKAGE_ROLES, user({"roles": roles}), NO_PROJECT_ROLES)
    helper = resolved.roles["helper"]
    assert (helper.model, helper.effort) == ("haiku", "max")
    assert resolved.layers["helper"]["model"] == USER


def test_a_project_role_of_a_personal_roles_name_replaces_it_entirely() -> None:
    roles = {"effort": "low", "helper": {**HELPER, "model": "opus"}}
    resolved = resolve_roles(
        PACKAGE_ROLES, user({"roles": roles}), project({"helper": {"instructions": "Ours."}})
    )
    assert resolved.roles["helper"] == Role(instructions="Ours.", effort="low")
    assert resolved.layers["helper"]["instructions"] == PROJECT
    assert list(resolved.roles) == [*PACKAGE_ROLES, "helper"]
    assert (resolved.personal, resolved.replaced) == (frozenset(), ("helper",))


def test_the_notice_of_a_replaced_personal_role_names_it() -> None:
    assert replaced_note("helper") == (
        "The project's role helper replaces your personal role of that name."
    )


@pytest.mark.parametrize(
    ("helper", "problem"),
    [
        (
            {"instructions": "You help.", "lane": "helper"},
            'roles: the enabled roles with workspace = "worktree" must share one lane, but lane'
            " 'work' has ['coder', 'configurator'] and lane 'helper' has ['helper']",
        ),
        (
            {**HELPER, "lane": "planner"},
            "roles: the conversational role 'planner' can't share its lane 'planner' with the task"
            " roles ['helper']",
        ),
    ],
)
def test_a_personal_role_breaking_a_rule_across_the_roles_is_a_personal_role_error(
    helper: dict[str, object], problem: str
) -> None:
    with pytest.raises(PersonalRoleError) as raised:
        resolve_roles(PACKAGE_ROLES, user({"roles": {"helper": helper}}), NO_PROJECT_ROLES)
    assert str(raised.value) == problem


def test_the_projects_own_clash_isnt_blamed_on_a_personal_role() -> None:
    clashing = {"instructions": "Audit.", "lane": "audit"}
    resolved = resolve_roles(
        PACKAGE_ROLES,
        user({"roles": {"helper": {"instructions": "You help.", "lane": "helper"}}}),
        project({"auditor": clashing}),
    )
    assert resolved.personal == {"helper"}


def test_a_role_only_the_user_config_names_isnt_a_role() -> None:
    resolved = resolve_roles(
        PACKAGE_ROLES, user({"roles": {"ghost": {"model": "opus"}}}), NO_PROJECT_ROLES
    )
    assert "ghost" not in resolved.roles


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ({"coder": {"tools": ["launch_rockets"]}}, "roles.coder.tools: "),
        ({"coder": "everything"}, "roles.coder: "),
        ({"new": {"tools": ["get_task"]}}, "roles.new.instructions: "),
    ],
)
def test_an_invalid_project_role_is_refused_naming_it_and_the_field(
    roles: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=f"^{message}"):
        resolve_roles(PACKAGE_ROLES, user({"roles": {"model": "haiku"}}), project(roles))


# Merging the roles without validating them


def test_merged_roles_are_the_resolved_roles_tables_with_their_layers() -> None:
    roles = {"model": "haiku", "helper": HELPER}
    layers = (PACKAGE_ROLES, user({"roles": roles}), project({PLANNER: {"model": "opus"}}))
    merged, resolved = merge_roles(*layers), resolve_roles(*layers)
    assert {name: Role.model_validate(table) for name, table in merged.tables.items()} == (
        resolved.roles
    )
    assert merged.layers == resolved.layers
    assert (merged.personal, merged.replaced) == (resolved.personal, resolved.replaced)


def test_merged_roles_keep_an_invalid_project_role_as_given() -> None:
    roles: dict[str, object] = {"coder": {"tools": ["launch_rockets"]}, "new": "everything"}
    merged = merge_roles(PACKAGE_ROLES, NO_USER_ROLES, project(roles))
    assert merged.tables["coder"]["tools"] == ["launch_rockets"]
    assert merged.layers["coder"]["tools"] == PROJECT
    assert (merged.tables["new"], merged.layers["new"]) == ("everything", {})


def test_a_merged_personal_role_breaking_a_rule_across_the_roles_is_a_personal_role_error() -> None:
    helper = {"instructions": "You help.", "lane": "helper"}
    merged = merge_roles(PACKAGE_ROLES, user({"roles": {"helper": helper}}), NO_PROJECT_ROLES)
    with pytest.raises(PersonalRoleError, match=r"^roles: the enabled roles"):
        check_personal(merged)


def test_the_personal_roles_check_leaves_an_invalid_role_to_be_reported_with_the_rest() -> None:
    helper = {"instructions": "You help.", "lane": "helper"}
    merged = merge_roles(
        PACKAGE_ROLES,
        user({"roles": {"helper": helper}}),
        project({"coder": {"tools": ["launch_rockets"]}}),
    )
    check_personal(merged)
