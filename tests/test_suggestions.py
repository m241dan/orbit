"""How an agent's suggested or proposed task becomes a coder's task, or is refused."""

import pytest

from orbit.agent.domain.roles import built_in_roles
from orbit.agent.domain.suggestions import suggested_task, suggestion_refusal, task_suggestion
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES, Settings, TaskSpec

INSTRUCTIONS = {name: f"You are orbit's {name}." for name in BUILT_IN_ROLE_NAMES}
BUILT_IN_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)


def test_a_suggested_task_is_a_coders_titled_prompt() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert suggested_task(coder, " Add the flag \n", "\nAdd --fast. ") == TaskSpec(
        prompt="Add the flag\n\nAdd --fast.", settings=Settings(tool_access="full")
    )


def test_a_suggested_task_without_a_prompt_is_just_its_title() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert suggested_task(coder, "Add the flag", " \n").prompt == "Add the flag"


def test_a_suggested_task_names_no_skills_unless_given_them() -> None:
    coder = BUILT_IN_ROLES["coder"]
    assert suggested_task(coder, "Add the flag", "Add --fast.").skills == ()
    task = suggested_task(coder, "Add the flag", "Add --fast.", ("python", "application"))
    assert task.skills == ("python", "application")


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


def test_a_suggested_task_is_refused_while_the_coder_is_disabled_naming_it() -> None:
    coder = BUILT_IN_ROLES["coder"].model_copy(update={"enabled": False})
    refusal = suggestion_refusal({**BUILT_IN_ROLES, "coder": coder})
    assert refusal == "coder is disabled in this project's configuration."


def test_a_suggested_task_is_not_refused_while_the_coder_is_enabled() -> None:
    assert suggestion_refusal(BUILT_IN_ROLES) is None
