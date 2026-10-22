import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.roles import (
    ORBIT_TOOLS,
    Role,
    built_in_roles,
    out_of_time,
    role_task,
    suggested_task,
    task_suggestion,
    timeout_ending,
    tool_rules,
)
from orbit.protocol import (
    AGENT_FAILED,
    BUILT_IN_ROLE_NAMES,
    TIMED_OUT,
    Settings,
    TaskSpec,
    ToolAccess,
)

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(INSTRUCTIONS)


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
    with pytest.raises(ConfigError, match="planner"):
        built_in_roles(instructions)


def test_the_configurator_proposes_checks_without_editing_or_being_verified() -> None:
    configurator = BUILT_IN_ROLES["configurator"]
    assert configurator.tool_access == "restricted" and not configurator.verify
    assert configurator.tools == ("get_task", "propose_checks", "report_progress", "give_up")
    assert "propose_checks" in ORBIT_TOOLS


def test_a_roles_settings_are_its_model_and_tool_access() -> None:
    role = Role(instructions="Do it.", model="opus", tool_access="full")
    assert role.settings == Settings(model="opus", tool_access="full")
    assert BUILT_IN_ROLES["reviewer"].settings == Settings()


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
