from typing import Any

import pytest
from pydantic import ValidationError

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import (
    ROLE_KEYS,
    Role,
    RoleTools,
    ServerGrant,
    built_in_roles,
    check_enabled,
    disabled_roles,
    edits_files,
    enabled_roles,
    load_built_in_roles,
    names_file,
    old_tools_problems,
    role_setting,
    role_task,
)
from orbit.agent.domain.runtimes import CLAUDE
from orbit.agent.domain.tools import ORBIT_TOOLS, PROPOSE_TASK, orbit_tool_grant
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import (
    BUILT_IN_ROLE_NAMES,
    DEFAULT_RUNTIME,
    LENS_REVIEWERS,
    NOT_CONFIGURED,
    SUPER_LENS_REVIEWERS,
    Effort,
    Settings,
    TaskSpec,
    WorkspaceLevel,
)

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)


def role_tables() -> dict[str, dict[str, Any]]:
    """A ``roles.toml``'s tables for the built-in roles, each told its Markdown file."""
    return {name: {"instructions": f"{name}.md"} for name in BUILT_IN_ROLE_NAMES}


def test_the_built_in_roles_load_in_order_told_their_markdown_files() -> None:
    roles = load_built_in_roles({"roles": dict(reversed(role_tables().items()))})
    assert tuple(roles) == BUILT_IN_ROLE_NAMES
    assert all(role == Role(instructions=f"{name}.md") for name, role in roles.items())


def test_a_built_in_roles_fields_load_from_its_table() -> None:
    tables = role_tables()
    tables["reviewer"] |= {
        "model": "opus",
        "tools": {"orbit": ["get_task", "run_check"], "workspace": "shell"},
        "verify": False,
        "timeout_minutes": 5,
    }
    assert load_built_in_roles({"roles": tables})["reviewer"] == Role(
        instructions="reviewer.md",
        model="opus",
        tools=RoleTools(orbit=("get_task", "run_check"), workspace="shell"),
        verify=False,
        timeout_minutes=5,
    )


def test_an_unknown_field_in_a_built_in_role_raises_config_error_naming_it() -> None:
    tables = role_tables()
    tables["coder"]["colour"] = "blue"
    with pytest.raises(ConfigError, match=r"^roles\.coder\.colour: Extra inputs"):
        load_built_in_roles({"roles": tables})


@pytest.mark.parametrize(
    ("data", "key"),
    [({}, "roles"), ({"roles": "coder"}, "roles"), ({"roles": {}, "colour": "blue"}, "colour")],
)
def test_a_roles_toml_not_of_role_tables_raises_config_error_naming_the_key(
    data: dict[str, Any], key: str
) -> None:
    with pytest.raises(ConfigError, match=f"^{key}: "):
        load_built_in_roles(data)


def test_a_built_in_role_without_a_table_raises_config_error_naming_it() -> None:
    tables = role_tables()
    del tables["planner"]
    with pytest.raises(ConfigError, match=r"^roles: no table for the built-in roles \['planner'\]"):
        load_built_in_roles({"roles": tables})


def test_a_table_for_a_role_not_built_in_raises_config_error_naming_it() -> None:
    tables = role_tables() | {"tester": {"instructions": "tester.md"}}
    with pytest.raises(ConfigError, match=r"^roles: \['tester'\] aren't built-in roles$"):
        load_built_in_roles({"roles": tables})


def test_a_role_leaving_out_where_and_how_it_works_gets_the_coders_defaults() -> None:
    role = Role(instructions="Do it.")
    assert (role.lane, role.workspace) == ("work", "worktree")
    assert (role.conversation, role.merge, role.verify, role.enabled) == (False, True, True, True)


def test_a_role_leaving_out_its_runtime_runs_on_claude() -> None:
    assert Role(instructions="Do it.").runtime == "claude"


def test_a_role_leaving_out_its_runtime_runs_on_the_protocols_default_runtime() -> None:
    assert Role(instructions="Do it.").runtime == DEFAULT_RUNTIME == CLAUDE


def test_the_built_in_roles_run_on_claude() -> None:
    assert {role.runtime for role in BUILT_IN_ROLES.values()} == {"claude"}


TALKER: dict[str, Any] = {
    "conversation": True,
    "tools": RoleTools(orbit=(PROPOSE_TASK,)),
    "verify": False,
    "merge": False,
    "workspace": "snapshot",
}
"""A conversational role's fields."""


@pytest.mark.parametrize("workspace", ["worktree", "snapshot", "checkout", "none"])
def test_an_unverified_role_doesnt_merge_by_default(workspace: str) -> None:
    fields = {"instructions": "Do it.", "verify": False, "workspace": workspace}
    assert not Role.model_validate(fields).merge


@pytest.mark.parametrize("workspace", ["snapshot", "checkout", "none"])
def test_a_verified_role_outside_the_worktree_is_refused_as_it_cant_merge(workspace: str) -> None:
    with pytest.raises(ValidationError, match="verify = true needs merge = true"):
        Role.model_validate({"instructions": "Do it.", "workspace": workspace})


@pytest.mark.parametrize("lane", ["work", "review-2", "my_lane", "Lane9"])
def test_a_lane_is_a_plain_name(lane: str) -> None:
    assert Role(instructions="Do it.", lane=lane).lane == lane


def test_a_built_in_role_breaking_a_rule_raises_config_error_naming_it() -> None:
    tables = role_tables()
    tables["reviewer"] |= {"workspace": "snapshot", "merge": True}
    with pytest.raises(
        ConfigError, match=r'^roles\.reviewer: merge = true needs workspace = "worktree"$'
    ):
        load_built_in_roles({"roles": tables})


def test_the_built_in_roles_are_told_their_instructions() -> None:
    roles = built_in_roles(load_built_in_roles({"roles": role_tables()}), INSTRUCTIONS)
    assert tuple(roles) == BUILT_IN_ROLE_NAMES
    assert all(role == Role(instructions=INSTRUCTIONS[name]) for name, role in roles.items())


def test_built_in_roles_cover_coding_reviewing_and_planning() -> None:
    assert set(BUILT_IN_ROLES) == {
        "coder",
        "reviewer",
        "planner",
        "experimenter",
        "configurator",
        *LENS_REVIEWERS,
        "style-reviewer",
        "drift-reviewer",
        "scaling-reviewer",
    }
    coder = BUILT_IN_ROLES["coder"]
    assert coder.verify and coder.workspace_level == "shell"
    assert set(coder.tools.orbit) == {
        "get_task",
        "complete_task",
        "run_check",
        "report_progress",
        "give_up",
    }
    reviewer = BUILT_IN_ROLES["reviewer"]
    assert not reviewer.verify and reviewer.workspace_level == "read"
    assert "run_check" not in reviewer.tools.orbit
    assert "suggest_task" in reviewer.tools.orbit
    assert "report_finding" in BUILT_IN_ROLES["drift-reviewer"].tools.orbit
    assert "suggest_task" not in BUILT_IN_ROLES["drift-reviewer"].tools.orbit
    assert "suggest_task" not in coder.tools.orbit
    planner = BUILT_IN_ROLES["planner"]
    assert set(planner.tools.orbit) == {
        "propose_task",
        "report_progress",
        "save_report",
        "list_reports",
        "read_report",
    }
    assert planner.consults == ("experimenter",)
    assert BUILT_IN_ROLES["drift-reviewer"].model == "opus"
    assert all(role.instructions == INSTRUCTIONS[name] for name, role in BUILT_IN_ROLES.items())
    assert all(set(role.tools.orbit) <= set(ORBIT_TOOLS) for role in BUILT_IN_ROLES.values())


@pytest.mark.parametrize("text", [None, "", "  \n"])
def test_every_built_in_role_needs_instructions(text: str | None) -> None:
    instructions = {name: "x" for name in BUILT_IN_ROLE_NAMES if name != "planner"}
    if text is not None:
        instructions["planner"] = text
    with pytest.raises(ConfigError, match=r"^no instructions for the built-in roles \['planner'\]"):
        built_in_roles(BUILT_IN_ROLES, instructions)


def test_the_configurator_proposes_checks_without_editing_or_being_verified() -> None:
    configurator = BUILT_IN_ROLES["configurator"]
    assert configurator.workspace_level == "read" and not configurator.verify
    assert configurator.tools.orbit == ("get_task", "propose_checks", "report_progress", "give_up")
    assert "propose_checks" in ORBIT_TOOLS


def test_a_roles_settings_are_its_model_workspace_level_web_and_effort() -> None:
    tools = RoleTools(workspace="write", web=False)
    role = Role(instructions="Do it.", model="opus", tools=tools, effort="high")
    assert role.settings == Settings(
        model="opus", workspace_level="write", web=False, effort="high"
    )
    assert BUILT_IN_ROLES["reviewer"].settings == Settings()


def test_a_role_leaving_out_its_tools_table_reads_with_web_and_the_task_tools() -> None:
    tools = Role(instructions="Do it.").tools
    assert tools == RoleTools(
        orbit=("get_task", "complete_task", "report_progress", "give_up"),
        workspace="read",
        web=True,
    )


@pytest.mark.parametrize(
    ("level", "web"), [("none", False), ("none", True), ("write", True), ("shell", False)]
)
def test_a_roles_tools_table_gives_its_agents_their_level_and_web(
    level: WorkspaceLevel, web: bool
) -> None:
    role = Role(instructions="Do it.", tools=RoleTools(workspace=level, web=web))
    assert (role.workspace_level, role.web) == (level, web)
    assert (role.settings.workspace_level, role.settings.web) == (level, web)


SHIPPED_LEVELS: dict[str, WorkspaceLevel] = {
    **dict.fromkeys(("planner", "reviewer", "configurator"), "read"),
    **dict.fromkeys((*LENS_REVIEWERS, *SUPER_LENS_REVIEWERS), "read"),
    "coder": "shell",
    "experimenter": "shell",
}
"""Each shipped role's workspace level, as its tool access gave it before; every one has web."""


def test_the_shipped_levels_cover_every_built_in_role() -> None:
    assert set(SHIPPED_LEVELS) == set(BUILT_IN_ROLE_NAMES)


@pytest.mark.parametrize(("name", "level"), SHIPPED_LEVELS.items())
def test_each_built_in_roles_settings_keep_the_level_and_web_it_had(
    name: str, level: WorkspaceLevel
) -> None:
    settings = BUILT_IN_ROLES[name].settings
    assert (settings.workspace_level, settings.web) == (level, True)


def test_a_roles_keys_split_its_tools_table_into_its_fields() -> None:
    assert ROLE_KEYS[-4:] == ("tools.orbit", "tools.workspace", "tools.web", "tools.mcp")
    assert "tools" not in ROLE_KEYS and ROLE_KEYS[0] == "instructions"


def test_a_roles_setting_is_its_fields_value_or_its_tables() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert role_setting(coder, "lane") == "work"
    assert role_setting(coder, "tools.workspace") == "shell"
    assert role_setting(coder, "tools.orbit") == coder.tools.orbit


@pytest.mark.parametrize(
    ("access", "form"),
    [
        ("off", 'workspace = "none", web = false'),
        ("restricted", 'workspace = "read", web = true'),
        ("full", 'workspace = "shell", web = true'),
        ("sometimes", 'workspace = "none", "read", "write" or "shell", and web = true or false'),
    ],
)
def test_a_roles_tool_access_is_refused_showing_its_workspace_and_web(
    access: str, form: str
) -> None:
    assert old_tools_problems({"coder": {"tool_access": access}}) == [
        f"roles.coder.tool_access is gone; write [roles.coder.tools] {form}."
    ]


def test_a_roles_list_of_tools_is_refused_showing_its_orbit_list() -> None:
    assert old_tools_problems({"coder": {"tools": ["get_task", "give_up"]}}) == [
        "roles.coder.tools is now a table; write [roles.coder.tools]"
        ' orbit = ["get_task", "give_up"].'
    ]


def test_each_old_form_is_refused_on_a_line_of_its_own_role_by_role() -> None:
    roles: dict[str, object] = {
        "coder": {"tool_access": "full", "tools": ["get_task"]},
        "reviewer": {"tools": {"workspace": "none"}},
        "planner": "everything",
        "helper": {"tool_access": "off"},
    }
    assert old_tools_problems(roles) == [
        "roles.coder.tool_access is gone; write [roles.coder.tools]"
        ' workspace = "shell", web = true.',
        'roles.coder.tools is now a table; write [roles.coder.tools] orbit = ["get_task"].',
        "roles.helper.tool_access is gone; write [roles.helper.tools]"
        ' workspace = "none", web = false.',
    ]


@pytest.mark.parametrize(
    ("level", "edits"),
    [("none", False), ("read", False), ("write", True), ("shell", True)],
)
def test_only_the_write_and_shell_levels_edit_files(level: WorkspaceLevel, edits: bool) -> None:
    assert edits_files(level) is edits


def test_a_role_leaving_out_effort_has_none() -> None:
    assert Role(instructions="Do it.").effort is None


def test_an_agent_runs_with_its_settings_own_effort_over_its_roles() -> None:
    settings = Settings(model="opus", workspace_level="shell", effort="high")
    assert Role(instructions="Do it.", effort="max").launch_settings(settings) == settings


@pytest.mark.parametrize("effort", ["max", None])
def test_an_agent_whose_settings_set_no_effort_runs_with_its_roles(effort: Effort | None) -> None:
    settings = Settings(model="opus", workspace_level="none", web=False)
    launched = Role(instructions="Do it.", effort=effort).launch_settings(settings)
    assert launched == Settings(model="opus", workspace_level="none", web=False, effort=effort)


@pytest.mark.parametrize(("minutes", "seconds"), [(2, 120), (0.5, 30), (None, None)])
def test_a_roles_timeout_in_seconds_is_its_minutes_or_no_limit(
    minutes: float | None, seconds: float | None
) -> None:
    assert Role(instructions="Do it.", timeout_minutes=minutes).timeout_seconds == seconds


def test_a_role_task_runs_in_the_role_with_its_settings() -> None:
    reviewer = BUILT_IN_ROLES["drift-reviewer"]
    assert role_task("drift-reviewer", reviewer, "Review.") == TaskSpec(
        prompt="Review.", settings=Settings(model="opus"), role="drift-reviewer"
    )


def test_a_role_task_carries_the_roles_effort() -> None:
    reviewer = Role(instructions="Review.", effort="xhigh")
    assert role_task("reviewer", reviewer, "Review.").settings.effort == "xhigh"


def test_a_role_task_names_no_skills_unless_given_them() -> None:
    reviewer = BUILT_IN_ROLES["reviewer"]
    assert role_task("reviewer", reviewer, "Review.").skills == ()
    assert role_task("reviewer", reviewer, "Review.", ("python",)).skills == ("python",)


def test_a_role_that_consults_and_proposes_is_granted_draft_tools_consult_and_read_skill() -> None:
    planner = BUILT_IN_ROLES["planner"]
    assert planner.granted_tools == (
        *planner.tools.orbit,
        "list_drafts",
        "read_draft",
        "edit_draft",
        "remove_draft",
        "move_draft",
        "consult",
        "read_skill",
    )


def test_a_role_without_propose_task_is_granted_no_draft_tools() -> None:
    reviewer = BUILT_IN_ROLES["reviewer"]
    assert reviewer.granted_tools == (*reviewer.tools.orbit, "read_skill")


@pytest.mark.parametrize("name", BUILT_IN_ROLE_NAMES)
def test_every_role_is_granted_read_skill_once(name: str) -> None:
    assert BUILT_IN_ROLES[name].granted_tools.count("read_skill") == 1


def test_a_role_with_no_tools_is_granted_read_skill_alone() -> None:
    role = Role(
        instructions="Plan.",
        tools=RoleTools(orbit=()),
        conversation=True,
        verify=False,
        workspace="none",
    )
    assert role.granted_tools == ("read_skill",)


def test_read_skill_among_a_roles_tools_is_refused() -> None:
    message = "read_skill comes with every role; leave it out of tools."
    with pytest.raises(ValidationError, match=message):
        RoleTools(orbit=("get_task", "read_skill"))


def test_a_draft_tool_among_a_roles_tools_is_refused() -> None:
    message = "read_draft comes with propose_task; list that instead."
    with pytest.raises(ValidationError, match=message):
        RoleTools(orbit=("propose_task", "read_draft"))


def test_a_role_that_consults_no_one_is_granted_its_tools_and_read_skill() -> None:
    experimenter = BUILT_IN_ROLES["experimenter"]
    assert experimenter.granted_tools == (*experimenter.tools.orbit, "read_skill")


def test_consult_among_a_roles_tools_is_refused() -> None:
    with pytest.raises(ValidationError, match="consult comes with consults, not tools"):
        RoleTools(orbit=("consult",))


@pytest.mark.parametrize(
    ("tools", "runs", "proposes"),
    [
        (("get_task",), False, False),
        (("get_task", "run_check"), True, False),
        (("get_task", "propose_checks"), False, True),
    ],
)
def test_a_roles_capabilities_follow_its_tools(
    tools: tuple[str, ...], runs: bool, proposes: bool
) -> None:
    role = Role(instructions="Work.", tools=RoleTools(orbit=tools))
    assert role.runs_checks is runs
    assert role.proposes_checks is proposes
    assert not role.proposes_tasks


def test_a_role_holding_propose_task_proposes_tasks() -> None:
    assert Role(instructions="Plan.", **TALKER).proposes_tasks


@pytest.mark.parametrize("name", BUILT_IN_ROLE_NAMES)
def test_each_built_in_roles_capabilities_follow_its_tools(name: str) -> None:
    role = BUILT_IN_ROLES[name]
    assert role.runs_checks is ("run_check" in role.tools.orbit)
    assert role.proposes_checks is ("propose_checks" in role.tools.orbit)
    assert role.proposes_tasks is ("propose_task" in role.tools.orbit)


def test_the_disabled_roles_are_those_with_enabled_false() -> None:
    reviewer = BUILT_IN_ROLES["reviewer"].model_copy(update={"enabled": False})
    assert disabled_roles(BUILT_IN_ROLES) == frozenset()
    assert disabled_roles({**BUILT_IN_ROLES, "reviewer": reviewer}) == frozenset({"reviewer"})


def test_the_enabled_roles_are_the_others_by_name_in_their_order() -> None:
    reviewer = BUILT_IN_ROLES["reviewer"].model_copy(update={"enabled": False})
    enabled = enabled_roles({**BUILT_IN_ROLES, "reviewer": reviewer})
    assert enabled == {name: r for name, r in BUILT_IN_ROLES.items() if name != "reviewer"}
    assert list(enabled) == [name for name in BUILT_IN_ROLES if name != "reviewer"]


def test_work_in_a_disabled_role_is_refused_as_not_configured_naming_it() -> None:
    coder = BUILT_IN_ROLES["coder"].model_copy(update={"enabled": False})
    with pytest.raises(Refused) as refused:
        check_enabled({**BUILT_IN_ROLES, "coder": coder}, "coder")
    assert (refused.value.code, refused.value.message) == (
        NOT_CONFIGURED,
        "coder is disabled in this project's configuration.",
    )


@pytest.mark.parametrize("name", ["coder", "nobody"])
def test_work_in_an_enabled_role_or_one_there_is_none_of_is_not_refused(name: str) -> None:
    check_enabled(BUILT_IN_ROLES, name)


@pytest.mark.parametrize(
    ("instructions", "names"),
    [
        ("coder.md", True),
        ("roles/coder.md", True),
        ("Review for security.", False),
        ("Read the notes in\nREADME.md", False),
        ("Follow coder.md closely.", False),
    ],
)
def test_instructions_name_a_file_when_they_are_one_line_ending_in_md(
    instructions: str, names: bool
) -> None:
    assert names_file(instructions) is names


def test_a_planners_orbit_tool_grant_gives_its_draft_tools_and_withholds_none_of_them() -> None:
    grant = orbit_tool_grant(BUILT_IN_ROLES["planner"].granted_tools)
    drafts = {"list_drafts", "read_draft"}
    assert drafts <= set(grant.granted) and not drafts & set(grant.withheld)


# A role's MCP servers


def test_a_role_uses_no_mcp_servers_by_default() -> None:
    assert RoleTools().mcp == ()


def test_a_servers_name_alone_allows_all_its_tools() -> None:
    tools = RoleTools.model_validate({"mcp": ["semgrep"]})
    assert tools.mcp == (ServerGrant(server="semgrep"),)
    assert tools.mcp[0].tools is None


def test_a_servers_table_allows_the_tools_it_lists() -> None:
    tools = RoleTools.model_validate({"mcp": [{"server": "github", "tools": ["get_issue"]}]})
    assert tools.mcp == (ServerGrant(server="github", tools=("get_issue",)),)


def test_a_roles_servers_are_written_back_as_a_file_gives_them() -> None:
    given = ["semgrep", {"server": "github", "tools": ["get_issue", "list_issues"]}]
    tools = RoleTools.model_validate({"mcp": given})
    assert tools.model_dump(mode="json")["mcp"] == given
    assert RoleTools.model_validate(tools.model_dump()) == tools


def test_a_server_listed_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="github is listed more than once"):
        RoleTools.model_validate({"mcp": ["github", {"server": "github", "tools": ["get_issue"]}]})


def test_a_servers_empty_tools_list_is_refused() -> None:
    with pytest.raises(ValidationError, match="a server's tools name at least one"):
        RoleTools.model_validate({"mcp": [{"server": "github", "tools": []}]})


def test_a_servers_table_refuses_fields_it_doesnt_have() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RoleTools.model_validate({"mcp": [{"server": "github", "approve": True}]})
