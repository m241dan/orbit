from typing import Any

import pytest
from pydantic import ValidationError

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.roles import (
    ORBIT_TOOLS,
    Role,
    built_in_roles,
    check_enabled,
    conversational_roles,
    disabled_roles,
    load_built_in_roles,
    merging_lane,
    names_file,
    out_of_time,
    review_lane,
    role_task,
    suggested_task,
    suggestion_refusal,
    task_lanes,
    task_suggestion,
    timeout_ending,
    tool_rules,
)
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    NOT_CONFIGURED,
    TIMED_OUT,
    WORK_LANE,
    Effort,
    Settings,
    TaskSpec,
    ToolAccess,
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
        "tool_access": "full",
        "tools": ["get_task", "run_check"],
        "verify": False,
        "timeout_minutes": 5,
    }
    assert load_built_in_roles({"roles": tables})["reviewer"] == Role(
        instructions="reviewer.md",
        model="opus",
        tool_access="full",
        tools=("get_task", "run_check"),
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
        "super-reviewer",
        "planner",
        "configurator",
    }
    coder = BUILT_IN_ROLES["coder"]
    assert coder.verify and coder.tool_access == "full"
    assert set(coder.tools) == {
        "get_task",
        "complete_task",
        "run_check",
        "report_progress",
        "give_up",
    }
    reviewer = BUILT_IN_ROLES["reviewer"]
    assert not reviewer.verify and reviewer.tool_access == "restricted"
    assert "run_check" not in reviewer.tools
    assert "suggest_task" in reviewer.tools
    assert "suggest_task" in BUILT_IN_ROLES["super-reviewer"].tools
    assert "suggest_task" not in coder.tools
    planner = BUILT_IN_ROLES["planner"]
    assert set(planner.tools) == {"propose_task", "report_progress"}
    assert BUILT_IN_ROLES["super-reviewer"].model == "opus"
    assert all(role.instructions == INSTRUCTIONS[name] for name, role in BUILT_IN_ROLES.items())
    assert all(set(role.tools) <= set(ORBIT_TOOLS) for role in BUILT_IN_ROLES.values())


@pytest.mark.parametrize("text", [None, "", "  \n"])
def test_every_built_in_role_needs_instructions(text: str | None) -> None:
    instructions = {name: "x" for name in BUILT_IN_ROLE_NAMES if name != "planner"}
    if text is not None:
        instructions["planner"] = text
    with pytest.raises(ConfigError, match=r"^no instructions for the built-in roles \['planner'\]"):
        built_in_roles(BUILT_IN_ROLES, instructions)


def test_the_configurator_proposes_checks_without_editing_or_being_verified() -> None:
    configurator = BUILT_IN_ROLES["configurator"]
    assert configurator.tool_access == "restricted" and not configurator.verify
    assert configurator.tools == ("get_task", "propose_checks", "report_progress", "give_up")
    assert "propose_checks" in ORBIT_TOOLS


def test_a_roles_settings_are_its_model_tool_access_and_effort() -> None:
    role = Role(instructions="Do it.", model="opus", tool_access="full", effort="high")
    assert role.settings == Settings(model="opus", tool_access="full", effort="high")
    assert BUILT_IN_ROLES["reviewer"].settings == Settings()


def test_a_role_leaving_out_effort_has_none() -> None:
    assert Role(instructions="Do it.").effort is None


def test_an_agent_runs_with_its_settings_own_effort_over_its_roles() -> None:
    settings = Settings(model="opus", tool_access="full", effort="high")
    assert Role(instructions="Do it.", effort="max").launch_settings(settings) == settings


@pytest.mark.parametrize("effort", ["max", None])
def test_an_agent_whose_settings_set_no_effort_runs_with_its_roles(effort: Effort | None) -> None:
    settings = Settings(model="opus", tool_access="off")
    launched = Role(instructions="Do it.", effort=effort).launch_settings(settings)
    assert launched == Settings(model="opus", tool_access="off", effort=effort)


@pytest.mark.parametrize(("minutes", "seconds"), [(2, 120), (0.5, 30), (None, None)])
def test_a_roles_timeout_in_seconds_is_its_minutes_or_no_limit(
    minutes: float | None, seconds: float | None
) -> None:
    assert Role(instructions="Do it.", timeout_minutes=minutes).timeout_seconds == seconds


@pytest.mark.parametrize(("minutes", "shown"), [(2, "2"), (2.0, "2"), (0.5, "0.5")])
def test_out_of_time_names_who_and_their_minutes(minutes: float, shown: str) -> None:
    assert out_of_time("agent-3", minutes) == f"agent-3 ran out of time ({shown} minutes)."


def test_a_timeout_after_the_deadline_expired_is_running_out_of_time() -> None:
    ending = timeout_ending(True, 2, "agent-3", TimeoutError("pipe timed out"))
    assert ending == (TIMED_OUT, "agent-3 ran out of time (2 minutes).")


@pytest.mark.parametrize(("expired", "minutes"), [(False, 2), (False, None), (True, None)])
def test_a_timeout_the_deadline_didnt_cause_is_a_failure(
    expired: bool, minutes: float | None
) -> None:
    ending = timeout_ending(expired, minutes, "agent-3", TimeoutError("pipe timed out"))
    assert ending == (AGENT_FAILED, "pipe timed out")


def test_a_role_task_runs_in_the_role_with_its_settings() -> None:
    reviewer = BUILT_IN_ROLES["super-reviewer"]
    assert role_task("super-reviewer", reviewer, "Review.") == TaskSpec(
        prompt="Review.", settings=Settings(model="opus"), role="super-reviewer"
    )


def test_a_role_task_carries_the_roles_effort() -> None:
    reviewer = Role(instructions="Review.", effort="xhigh")
    assert role_task("reviewer", reviewer, "Review.").settings.effort == "xhigh"


def test_a_suggested_task_is_a_coders_titled_prompt() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert suggested_task(coder, " Add the flag \n", "\nAdd --fast. ") == TaskSpec(
        prompt="Add the flag\n\nAdd --fast.", settings=Settings(tool_access="full")
    )


def test_a_suggested_task_without_a_prompt_is_just_its_title() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert suggested_task(coder, "Add the flag", " \n").prompt == "Add the flag"


def test_a_task_suggestion_is_its_stripped_title_and_prompt() -> None:
    arguments = {"title": " Add the flag ", "prompt": "\nAdd --fast. "}
    assert task_suggestion(arguments) == ("Add the flag", "Add --fast.")


def test_a_task_suggestions_title_is_flattened_to_one_line() -> None:
    arguments = {"title": "Fix A\n  and\tB\n", "prompt": "Both."}
    assert task_suggestion(arguments) == ("Fix A and B", "Both.")


@pytest.mark.parametrize("title", ["", " \n\t "])
def test_a_task_suggestion_with_a_blank_title_is_refused(title: str) -> None:
    assert task_suggestion({"title": title, "prompt": "Do X"}) == "A task needs a title."


def test_a_task_suggestion_without_a_title_is_refused() -> None:
    assert task_suggestion({"prompt": "Do X"}) == "A task needs a title."


def test_a_task_suggestion_without_a_prompt_has_an_empty_one() -> None:
    assert task_suggestion({"title": "Tidy"}) == ("Tidy", "")


def test_orbit_tools_are_the_eight_in_order() -> None:
    assert ORBIT_TOOLS == (
        "get_task",
        "complete_task",
        "run_check",
        "report_progress",
        "give_up",
        "suggest_task",
        "propose_task",
        "propose_checks",
    )


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
    role = Role(instructions="Work.", tools=tools)
    assert role.runs_checks is runs
    assert role.proposes_checks is proposes


@pytest.mark.parametrize("name", BUILT_IN_ROLE_NAMES)
def test_each_built_in_roles_capabilities_follow_its_tools(name: str) -> None:
    role = BUILT_IN_ROLES[name]
    assert role.runs_checks is ("run_check" in role.tools)
    assert role.proposes_checks is ("propose_checks" in role.tools)


@pytest.mark.parametrize("access", ["off", "full"])
def test_unrestricted_tool_rules_allow_the_roles_orbit_tools_and_deny_its_others(
    access: ToolAccess,
) -> None:
    rules = tool_rules(access, ("get_task", "complete_task"))
    assert rules.allowed == ("mcp__orbit__get_task", "mcp__orbit__complete_task")
    assert rules.denied == (
        "mcp__orbit__run_check",
        "mcp__orbit__report_progress",
        "mcp__orbit__give_up",
        "mcp__orbit__suggest_task",
        "mcp__orbit__propose_task",
        "mcp__orbit__propose_checks",
    )


def test_restricted_tool_rules_add_reading_and_searching_and_deny_writing() -> None:
    rules = tool_rules("restricted", ("propose_task", "report_progress"))
    assert rules.allowed == (
        "mcp__orbit__propose_task",
        "mcp__orbit__report_progress",
        "Read",
        "Glob",
        "Grep",
        "WebSearch",
    )
    assert rules.denied == (
        "mcp__orbit__get_task",
        "mcp__orbit__complete_task",
        "mcp__orbit__run_check",
        "mcp__orbit__give_up",
        "mcp__orbit__suggest_task",
        "mcp__orbit__propose_checks",
        "Edit",
        "Write",
        "NotebookEdit",
    )


def test_tool_rules_for_every_orbit_tool_deny_none_unless_restricted() -> None:
    assert tool_rules("full", ORBIT_TOOLS).denied == ()
    assert tool_rules("restricted", ORBIT_TOOLS).denied == ("Edit", "Write", "NotebookEdit")


def test_the_built_in_task_roles_run_in_the_work_and_review_lanes() -> None:
    assert task_lanes(BUILT_IN_ROLES) == {
        "work": frozenset({"coder", "configurator"}),
        "review": frozenset({"reviewer", "super-reviewer"}),
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


def test_the_built_in_conversational_role_is_the_planner() -> None:
    assert conversational_roles(BUILT_IN_ROLES) == ("planner",)


def test_the_conversational_roles_are_the_enabled_ones_in_conversation_in_order() -> None:
    architect = BUILT_IN_ROLES["planner"].model_copy(update={"lane": "design"})
    muted = architect.model_copy(update={"lane": "muted", "enabled": False})
    roles = {"architect": architect, **BUILT_IN_ROLES, "muted": muted}
    assert conversational_roles(roles) == ("architect", "planner")


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


def test_the_disabled_roles_are_those_with_enabled_false() -> None:
    reviewer = BUILT_IN_ROLES["reviewer"].model_copy(update={"enabled": False})
    assert disabled_roles(BUILT_IN_ROLES) == frozenset()
    assert disabled_roles({**BUILT_IN_ROLES, "reviewer": reviewer}) == frozenset({"reviewer"})


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


def test_a_suggested_task_is_refused_while_the_coder_is_disabled_naming_it() -> None:
    coder = BUILT_IN_ROLES["coder"].model_copy(update={"enabled": False})
    refusal = suggestion_refusal({**BUILT_IN_ROLES, "coder": coder})
    assert refusal == "coder is disabled in this project's configuration."


def test_a_suggested_task_is_not_refused_while_the_coder_is_enabled() -> None:
    assert suggestion_refusal(BUILT_IN_ROLES) is None


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
