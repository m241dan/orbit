from dataclasses import replace
from typing import Any, get_args

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.role_runtimes import (
    capability_problems,
    check_model,
    check_runtimes,
    check_settings,
    runtime_problems,
    runtime_roles,
)
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.domain.runtimes import CLAUDE, RuntimeRules
from orbit.agent.domain.tools import PROPOSE_TASK
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES, INVALID, Effort, Settings, ToolAccess
from tests.agent_fakes import CLAUDE_MODELS, EVERYTHING, KNOWN_RULES, LIMITED, LIMITED_RUNTIME

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)


def test_the_runtimes_the_enabled_roles_use_name_their_roles_in_order() -> None:
    roles = {
        "coder": Role(instructions="Code."),
        "helper": Role(instructions="Help.", runtime="fake"),
        "reviewer": Role(instructions="Review.", verify=False),
        "idle": Role(instructions="Rest.", runtime="other", enabled=False),
    }
    assert runtime_roles(roles) == {"claude": ("coder", "reviewer"), "fake": ("helper",)}


def test_roles_on_runtimes_this_version_knows_have_no_runtime_problems() -> None:
    assert runtime_problems(BUILT_IN_ROLES) == []


def test_each_role_on_a_runtime_this_version_doesnt_know_is_a_runtime_problem() -> None:
    roles = {**BUILT_IN_ROLES, "coder": Role(instructions="Code.", runtime="nope")}
    assert runtime_problems(roles) == [
        'roles.coder.runtime = "nope": this version of orbit knows the runtimes claude, kiro.'
    ]


def test_roles_refused_for_their_runtimes_raise_config_error_a_line_each() -> None:
    roles = {
        "coder": Role(instructions="Code.", runtime="nope"),
        "helper": Role(instructions="Help.", runtime="other"),
    }
    with pytest.raises(ConfigError) as error:
        check_runtimes(roles)
    assert str(error.value) == (
        'roles.coder.runtime = "nope": this version of orbit knows the runtimes claude, kiro.\n'
        'roles.helper.runtime = "other": this version of orbit knows the runtimes claude, kiro.'
    )


def test_roles_their_runtimes_take_raise_nothing() -> None:
    check_runtimes(BUILT_IN_ROLES, KNOWN_RULES)


# What a role asks of its runtime


def lite(**fields: Any) -> dict[str, Role]:
    """The role ``x`` on :data:`LIMITED_RUNTIME`, with ``fields``."""
    return {"x": Role(instructions="Do x.", runtime=LIMITED_RUNTIME, **fields)}


TALKER: dict[str, Any] = {
    "conversation": True,
    "tools": (PROPOSE_TASK,),
    "verify": False,
    "merge": False,
    "workspace": "snapshot",
}
"""A conversational role's fields."""


def test_the_built_in_roles_ask_nothing_claude_cant_do() -> None:
    assert runtime_problems(BUILT_IN_ROLES, RuntimeRules({CLAUDE: EVERYTHING})) == []


@pytest.mark.parametrize("effort", get_args(Effort))
@pytest.mark.parametrize("tool_access", get_args(ToolAccess))
def test_a_role_on_claude_may_ask_for_any_effort_or_tool_access(
    effort: Effort, tool_access: ToolAccess
) -> None:
    roles = {"x": Role(instructions="Do x.", effort=effort, tool_access=tool_access, **TALKER)}
    assert runtime_problems(roles, RuntimeRules({CLAUDE: EVERYTHING})) == []


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_for_effort_its_runtime_takes_none_of_is_refused() -> None:
    assert runtime_problems(lite(effort="high"), KNOWN_RULES) == [
        'roles.x asks for effort "high", but its runtime lite accepts no effort setting.'
    ]


@pytest.mark.usefixtures("limited_runtime")
@pytest.mark.parametrize(
    ("efforts", "accepted"),
    [
        ({"low"}, '"low"'),
        ({"high", "low"}, '"low" or "high"'),
        ({"high", "medium", "low"}, '"low", "medium" or "high"'),
    ],
)
def test_a_role_asking_for_an_effort_its_runtime_doesnt_take_is_refused_listing_those_it_does(
    efforts: set[Effort], accepted: str
) -> None:
    capabilities = {LIMITED_RUNTIME: replace(LIMITED, effort=frozenset(efforts))}
    assert runtime_problems(lite(effort="max"), RuntimeRules(capabilities)) == [
        f'roles.x asks for effort "max", but its runtime lite accepts only effort {accepted}.'
    ]


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_for_tool_access_its_runtime_doesnt_take_is_refused_listing_it() -> None:
    assert runtime_problems(lite(tool_access="full"), KNOWN_RULES) == [
        'roles.x asks for tool_access "full", but its runtime lite accepts only tool_access'
        ' "restricted".'
    ]


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_for_tool_access_from_a_runtime_taking_none_is_refused() -> None:
    capabilities = {LIMITED_RUNTIME: replace(LIMITED, tool_access=frozenset())}
    assert runtime_problems(lite(), RuntimeRules(capabilities)) == [
        'roles.x asks for tool_access "restricted", but its runtime lite accepts no tool_access'
        " setting."
    ]


@pytest.mark.usefixtures("limited_runtime")
def test_a_conversational_role_on_a_runtime_that_cant_resume_is_refused_naming_resume() -> None:
    assert runtime_problems(lite(**TALKER), KNOWN_RULES) == [
        "roles.x asks for conversation = true, but its runtime lite doesn't support resume."
    ]


@pytest.mark.usefixtures("limited_runtime")
def test_everything_a_role_asks_beyond_its_runtime_is_named_in_order() -> None:
    roles = {
        **lite(tool_access="off", effort="low", **TALKER),
        "coder": Role(instructions="Code.", runtime=LIMITED_RUNTIME, tool_access="full"),
    }
    problems = runtime_problems(roles, KNOWN_RULES)
    assert [problem.split(", but")[0] for problem in problems] == [
        'roles.x asks for tool_access "off"',
        'roles.x asks for effort "low"',
        "roles.x asks for conversation = true",
        'roles.coder asks for tool_access "full"',
    ]


def test_a_roles_asks_in_only_the_fields_given_are_named_in_their_usual_order() -> None:
    role = lite(tool_access="off", effort="low", **TALKER)["x"]
    problems = capability_problems("x", role, LIMITED, ("conversation", "tool_access"))
    assert [problem.split(", but")[0] for problem in problems] == [
        'roles.x asks for tool_access "off"',
        "roles.x asks for conversation = true",
    ]


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_only_what_its_runtime_can_do_has_no_runtime_problems() -> None:
    assert runtime_problems(lite(tool_access="restricted"), KNOWN_RULES) == []


@pytest.mark.usefixtures("limited_runtime")
def test_a_disabled_role_isnt_held_to_its_runtimes_capabilities() -> None:
    roles = lite(effort="max", tool_access="full", enabled=False)
    assert runtime_problems(roles, KNOWN_RULES) == []


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_on_a_runtime_whose_capabilities_arent_given_isnt_held_to_them() -> None:
    assert runtime_problems(lite(effort="max")) == []
    assert runtime_problems(lite(effort="max"), RuntimeRules({CLAUDE: EVERYTHING})) == []


# The models a role, a task and a conversation ask for

CLAUDES_ALIASES = (
    "the runtime claude takes the aliases fable, opus, sonnet, haiku or a full id starting with"
    ' "claude-"'
)
"""What says which models Claude Code takes, as orbit ships its aliases."""


def on_claude(**fields: Any) -> dict[str, Role]:
    """The role ``x`` on Claude Code, with ``fields``."""
    return {"x": Role(instructions="Do x.", **fields)}


def test_a_role_whose_model_its_runtime_doesnt_take_is_refused_naming_the_aliases() -> None:
    assert runtime_problems(on_claude(model="sonet"), KNOWN_RULES) == [
        f'roles.x.model = "sonet": {CLAUDES_ALIASES}.'
    ]


@pytest.mark.parametrize("model", ["sonnet", "claude-sonnet-5-5", None])
def test_a_role_with_an_alias_a_full_id_or_no_model_has_no_model_problem(
    model: str | None,
) -> None:
    assert runtime_problems(on_claude(model=model), KNOWN_RULES) == []


def test_a_disabled_roles_model_is_still_held_to_its_runtimes() -> None:
    roles = on_claude(model="sonet", enabled=False)
    assert runtime_problems(roles, KNOWN_RULES) == [f'roles.x.model = "sonet": {CLAUDES_ALIASES}.']


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_on_a_runtime_whose_models_arent_given_isnt_held_to_them() -> None:
    assert runtime_problems(lite(model="anything"), KNOWN_RULES) == []
    assert runtime_problems(on_claude(model="anything")) == []


@pytest.mark.usefixtures("limited_runtime")
def test_a_roles_model_is_named_before_what_it_asks_beyond_its_runtime() -> None:
    lite_models = replace(CLAUDE_MODELS, runtime=LIMITED_RUNTIME)
    rules = RuntimeRules(KNOWN_RULES.capabilities, {LIMITED_RUNTIME: lite_models})
    problems = runtime_problems(lite(model="sonet", effort="low"), rules)
    assert [problem.split(":")[0].split(", but")[0] for problem in problems] == [
        'roles.x.model = "sonet"',
        'roles.x asks for effort "low"',
    ]


def test_a_tasks_model_its_roles_runtime_doesnt_take_is_refused_as_invalid() -> None:
    with pytest.raises(Refused) as refused:
        check_model("coder", "bogus", CLAUDE_MODELS)
    assert (refused.value.code, refused.value.message) == (
        INVALID,
        f"coder's model \"bogus\" doesn't resolve: {CLAUDES_ALIASES}.",
    )


@pytest.mark.parametrize("model", ["opus", "claude-opus-5-5", None])
def test_a_tasks_alias_full_id_or_default_model_is_not_refused(model: str | None) -> None:
    check_model("coder", model, CLAUDE_MODELS)


def test_a_tasks_model_isnt_refused_without_its_runtimes_models() -> None:
    check_model("coder", "bogus", None)


# The levels a task and a conversation ask for


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param(
            Settings(effort="high"),
            "Role coder's runtime lite accepts no effort setting, so it can't take effort"
            ' "high".',
            id="effort",
        ),
        pytest.param(
            Settings(tool_access="full"),
            "Role coder's runtime lite accepts only tool_access \"restricted\", so it can't take"
            ' tool_access "full".',
            id="tool_access",
        ),
    ],
)
def test_a_tasks_level_its_roles_runtime_doesnt_accept_is_refused_as_invalid(
    settings: Settings, message: str
) -> None:
    with pytest.raises(Refused) as refused:
        check_settings("coder", LIMITED_RUNTIME, settings, LIMITED)
    assert (refused.value.code, refused.value.message) == (INVALID, message)


def test_a_task_asking_beyond_its_runtime_for_both_levels_is_refused_for_its_tool_access() -> None:
    settings = Settings(tool_access="full", effort="high")
    with pytest.raises(Refused, match='tool_access "full"'):
        check_settings("coder", LIMITED_RUNTIME, settings, LIMITED)


def test_a_tasks_unset_effort_and_accepted_tool_access_are_not_refused() -> None:
    check_settings("coder", LIMITED_RUNTIME, Settings(tool_access="restricted"), LIMITED)
