"""What ``orbit config show`` reports: each setting in effect, with the layer it came from."""

import re
from dataclasses import replace
from typing import Any

import pytest

from orbit.agent.domain.config import LIMIT_KEYS, load_layered
from orbit.agent.domain.config_report import (
    ConfigReport,
    ConfigSources,
    Instructions,
    Setting,
    SkillFileText,
    UnknownKey,
    Written,
    config_report,
    subtree,
)
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer, load_user_roles
from orbit.agent.domain.roles import Role, built_in_roles
from orbit.agent.domain.server import ServerSettings
from orbit.agent.domain.skills import Skill, SkillFile, SkillLibrary
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES

type Data = dict[str, Any]

INSTRUCTIONS = {name: f"You are orbit's {name}.\nBe brief." for name in BUILT_IN_ROLE_NAMES}
PACKAGE_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)
PACKAGE_FILES = {name: f"{name}.md" for name in BUILT_IN_ROLE_NAMES}
USER = Layer("user", "/home/me/.config/orbit/config.toml")
PROJECT = Layer("project", "/src/app/.orbit/config.toml")
HELPER = {
    "instructions": "You help.",
    "lane": "helper",
    "workspace": "snapshot",
    "merge": False,
    "verify": False,
}
"""A personal role's table: a helper in a lane of its own, on a snapshot."""
SKILL_PATH = "/src/app/.orbit/skills/python/tests.md"
PROJECT_SKILL = Layer("project", SKILL_PATH)


def sources(
    project: Data | None = None,
    user: Data | None = None,
    project_read: Data | None = None,
    user_read: Data | None = None,
) -> ConfigSources:
    """The sources of the configuration a project file holding ``project`` and a user config
    holding ``user`` make, read as ``project_read`` and ``user_read``, with the text of the
    instructions files they name (each as written if ``None``)."""
    project, user = project or {}, user or {}
    user_roles = load_user_roles(user_read or user, USER, PACKAGE_ROLES)
    layered = load_layered(project_read or project, PACKAGE_ROLES, user_roles, PROJECT.path)
    return ConfigSources(
        layered.config,
        layered.role_layers,
        Written(PROJECT, project),
        Written(USER, user),
        PACKAGE_FILES,
    )


def report(project: Data | None = None, user: Data | None = None) -> ConfigReport:
    """What's reported of the configuration a project file holding ``project`` and a user
    config holding ``user`` make, with no table of the user config's own."""
    return config_report(sources(project, user), {})


def skill(name: str, files: dict[str, SkillFile]) -> Skill:
    """The skill ``name`` of ``files``, by their paths."""
    return Skill(name=name, description=f"The {name} skill.", body="", files=files)


def skill_file(text: str, layer: Layer = PACKAGE) -> SkillFile:
    """A skill file from ``layer`` holding ``text``."""
    return SkillFile(text=text, layer=layer, location=layer.path or "package")


LIBRARY = SkillLibrary(
    skills={
        "python": skill(
            "python",
            {
                "tests.md": skill_file("Test.\nWell.\n", PROJECT_SKILL),
                "SKILL.md": skill_file("# Python\n"),
            },
        ),
        "cpp": skill("cpp", {"scripts/run.sh": skill_file("run\n"), "SKILL.md": skill_file("")}),
    }
)
"""Two skills, given out of order, the python one with a file of the project's."""


def with_skills(project: Data | None = None) -> ConfigReport:
    """What's reported of the configuration a project file holding ``project`` makes with the
    skills of :data:`LIBRARY`."""
    given = sources(project)
    config = given.config.model_copy(update={"skill_library": LIBRARY})
    return config_report(replace(given, config=config), {})


# The limits


def test_the_limits_come_first_in_the_project_files_order() -> None:
    assert tuple(report())[: len(LIMIT_KEYS)] == LIMIT_KEYS
    assert LIMIT_KEYS == (
        "fix_attempts",
        "max_checks",
        "fallback_fixes",
        "review_rounds",
        "super_review_every",
        "review_suggestions",
        "super_review_suggestions",
    )


def test_a_limit_the_project_file_leaves_out_is_the_default() -> None:
    assert report()["fix_attempts"] == Setting(5, DEFAULT)


def test_a_limit_the_project_file_sets_is_the_projects() -> None:
    assert report({"max_checks": 7})["max_checks"] == Setting(7, PROJECT)


# The roles


def test_each_role_is_listed_field_by_field_in_order() -> None:
    roles = [key for key in report() if key.startswith("roles.")]
    assert roles == [
        f"roles.{name}.{field}" for name in BUILT_IN_ROLE_NAMES for field in Role.model_fields
    ]


def test_each_role_field_comes_from_the_layer_that_set_it() -> None:
    settings = report(
        {"roles": {"reviewer": {"model": "sonnet"}}}, {"roles": {"planner": {"model": "opus"}}}
    )
    assert settings["roles.reviewer.model"] == Setting("sonnet", PROJECT)
    assert settings["roles.planner.model"] == Setting("opus", USER)
    assert settings["roles.coder.tools"] == Setting(PACKAGE_ROLES["coder"].tools, PACKAGE)
    assert settings["roles.coder.effort"] == Setting(None, DEFAULT)


def test_the_users_model_for_every_role_is_the_users() -> None:
    assert report(user={"roles": {"model": "haiku"}})["roles.coder.model"] == Setting("haiku", USER)


def test_a_package_roles_instructions_are_in_the_file_it_names() -> None:
    instructions = Instructions(INSTRUCTIONS["reviewer"], "reviewer.md")
    assert report()["roles.reviewer.instructions"] == Setting(instructions, PACKAGE)


def test_instructions_the_project_file_gives_as_text_are_in_no_file() -> None:
    settings = report({"roles": {"coder": {"instructions": "Code."}}})
    assert settings["roles.coder.instructions"] == Setting(Instructions("Code.", None), PROJECT)


def test_instructions_the_project_file_names_a_file_for_are_in_that_file() -> None:
    written = {"roles": {"coder": {"instructions": "roles/coder.md"}}}
    read = {"roles": {"coder": {"instructions": "Code well."}}}
    settings = config_report(sources(written, project_read=read), {})
    instructions = Instructions("Code well.", "roles/coder.md")
    assert settings["roles.coder.instructions"] == Setting(instructions, PROJECT)


def test_a_personal_role_is_listed_with_its_fields_and_instructions_file_from_the_user() -> None:
    written = {"roles": {"helper": {**HELPER, "instructions": "roles/helper.md"}}}
    settings = config_report(sources(user=written, user_read={"roles": {"helper": HELPER}}), {})
    instructions = Instructions("You help.", "roles/helper.md")
    assert settings["roles.helper.instructions"] == Setting(instructions, USER)
    assert settings["roles.helper.lane"] == Setting("helper", USER)
    assert settings["roles.helper.tools"].layer == DEFAULT


@pytest.mark.parametrize(("text", "lines"), [("One.", 1), ("One.\nTwo.\n\nFour.", 4)])
def test_instructions_count_their_lines(text: str, lines: int) -> None:
    assert Instructions(text, None).lines == lines


# The verification steps and setup


def test_without_steps_there_are_none_by_default() -> None:
    assert report()["verification.steps"] == Setting((), DEFAULT)


def test_no_steps_the_project_file_gives_are_the_projects() -> None:
    assert report({"verification": {"steps": []}})["verification.steps"] == Setting((), PROJECT)


def test_each_step_the_project_file_gives_is_listed_field_by_field() -> None:
    steps = [
        {"name": "Lint", "command": ["ruff", "check"]},
        {"name": "Test", "command": ["pytest"]},
    ]
    settings = report({"verification": {"steps": steps}})
    assert {key: value for key, value in settings.items() if key.startswith("verification")} == {
        "verification.steps.1.name": Setting("Lint", PROJECT),
        "verification.steps.1.command": Setting(("ruff", "check"), PROJECT),
        "verification.steps.2.name": Setting("Test", PROJECT),
        "verification.steps.2.command": Setting(("pytest",), PROJECT),
    }


def test_without_a_setup_there_is_none_by_default() -> None:
    assert report()["setup"] == Setting(None, DEFAULT)


def test_the_setup_the_project_file_gives_is_listed_field_by_field() -> None:
    settings = report({"setup": {"command": ["uv", "sync"]}})
    assert settings["setup.command"] == Setting(("uv", "sync"), PROJECT)
    assert settings["setup.watch"] == Setting((), DEFAULT)
    assert "setup" not in settings


def test_the_project_files_sections_come_in_order() -> None:
    sections = [key.split(".")[0] for key in with_skills() if key not in LIMIT_KEYS]
    assert sorted(set(sections), key=sections.index) == [
        "roles",
        "verification",
        "setup",
        "skills",
        "skill_files",
    ]


# The skills


def test_without_skill_mappings_there_are_none_by_default() -> None:
    assert report()["skills"] == Setting((), DEFAULT)


def test_no_skill_mappings_the_project_file_gives_are_the_projects() -> None:
    assert report({"skills": []})["skills"] == Setting((), PROJECT)


def test_each_skill_mapping_the_project_file_gives_is_listed_field_by_field() -> None:
    mappings = [
        {"paths": ["src", "noxfile.py"], "use": ["python"]},
        {"paths": ["native"], "use": ["cpp", "security"]},
    ]
    settings = report({"skills": mappings})
    assert {key: value for key, value in settings.items() if key.startswith("skills")} == {
        "skills.1.paths": Setting(("src", "noxfile.py"), PROJECT),
        "skills.1.use": Setting(("python",), PROJECT),
        "skills.2.paths": Setting(("native",), PROJECT),
        "skills.2.use": Setting(("cpp", "security"), PROJECT),
    }


def test_each_skill_file_is_listed_by_skill_and_path_in_order_from_its_own_layer() -> None:
    assert {key: value for key, value in with_skills().items() if key.startswith("skill_")} == {
        "skill_files.cpp.SKILL.md": Setting(SkillFileText(""), PACKAGE),
        "skill_files.cpp.scripts/run.sh": Setting(SkillFileText("run\n"), PACKAGE),
        "skill_files.python.SKILL.md": Setting(SkillFileText("# Python\n"), PACKAGE),
        "skill_files.python.tests.md": Setting(SkillFileText("Test.\nWell.\n"), PROJECT_SKILL),
    }
    keys = [key for key in with_skills() if key.startswith("skill_")]
    assert keys == sorted(keys)


@pytest.mark.parametrize(("text", "lines"), [("", 0), ("One.\n", 1), ("One.\n\nThree.", 3)])
def test_a_skill_files_text_counts_its_lines(text: str, lines: int) -> None:
    assert SkillFileText(text).lines == lines


# The user config's tables


def test_each_table_of_the_user_config_is_listed_field_by_field_after_the_rest() -> None:
    tables = {"storage": StorageSettings(), "server": ServerSettings(port=9000)}
    settings = config_report(sources(user={"server": {"port": 9000}}), tables)
    assert settings["server.port"] == Setting(9000, USER)
    assert settings["server.host"] == Setting("127.0.0.1", DEFAULT)
    assert settings["storage.tool_output_kb"] == Setting(64, DEFAULT)
    keys = list(settings)
    assert keys[-len(StorageSettings.model_fields) - len(ServerSettings.model_fields) :] == [
        *(f"storage.{field}" for field in StorageSettings.model_fields),
        *(f"server.{field}" for field in ServerSettings.model_fields),
    ]


def test_a_table_the_user_config_gives_as_something_else_sets_nothing() -> None:
    settings = config_report(sources(user={"storage": 3}), {"storage": StorageSettings()})
    assert settings["storage.tool_output_kb"] == Setting(64, DEFAULT)


# A key's settings


def test_a_key_gives_the_settings_under_it() -> None:
    settings = report()
    assert list(subtree(settings, "roles.reviewer")) == [
        f"roles.reviewer.{field}" for field in Role.model_fields
    ]


def test_a_skills_key_gives_its_files_alone() -> None:
    assert list(subtree(with_skills(), "skill_files.python")) == [
        "skill_files.python.SKILL.md",
        "skill_files.python.tests.md",
    ]


def test_the_skills_key_gives_the_mappings_alone() -> None:
    mappings = [{"paths": ["src"], "use": ["python"]}]
    assert list(subtree(with_skills({"skills": mappings}), "skills")) == [
        "skills.1.paths",
        "skills.1.use",
    ]


def test_a_settings_own_key_gives_it_alone() -> None:
    assert subtree(report(), "fix_attempts") == {"fix_attempts": Setting(5, DEFAULT)}


@pytest.mark.parametrize("key", ["roles.nope", "roles.rev", "fix_attempts.more", ""])
def test_a_key_with_no_settings_under_it_is_unknown_naming_it(key: str) -> None:
    with pytest.raises(UnknownKey, match=f"^No setting {re.escape(key)} "):
        subtree(report(), key)
