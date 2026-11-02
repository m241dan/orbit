"""What ``orbit config show`` reports: each setting in effect, with the layer it came from."""

import re
from dataclasses import replace
from typing import Any

import pytest

from orbit.agent.domain.config import LIMIT_KEYS, Package, load_layered
from orbit.agent.domain.config_report import (
    ConfigReport,
    ConfigSources,
    Instructions,
    Setting,
    SkillFileText,
    SkillMap,
    UnknownKey,
    Written,
    config_report,
    key_parts,
    skill_map,
    subtree,
)
from orbit.agent.domain.layers import DEFAULT, PACKAGE, Layer, load_user_roles
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.roles import ROLE_KEYS, built_in_roles
from orbit.agent.domain.server import ServerSettings
from orbit.agent.domain.skills import Skill, SkillFile, SkillLibrary, SkillMapping
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.triggers import Trigger
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers, read_template_files
from orbit.protocol import BUILT_IN_ROLE_NAMES, LENS_REVIEWERS, SUPER_LENS_REVIEWERS

type Data = dict[str, Any]

INSTRUCTIONS = {name: f"You are orbit's {name}.\nBe brief." for name in BUILT_IN_ROLE_NAMES}
PACKAGE_ROLES = built_in_roles(read_built_in_roles(), INSTRUCTIONS)
PACKAGE_FILES = {name: f"{name}.md" for name in BUILT_IN_ROLE_NAMES}
PACKAGE_TRIGGERS = read_built_in_triggers()
TEMPLATE_FILES = read_template_files()
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
    package = Package(PACKAGE_ROLES, PACKAGE_TRIGGERS)
    layered = load_layered(project_read or project, package, user_roles, PROJECT.path)
    return ConfigSources(
        layered.config,
        layered.role_layers,
        Written(PROJECT, project),
        Written(USER, user),
        PACKAGE_FILES,
        layered.trigger_layers,
        TEMPLATE_FILES,
        layered.server_layers,
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


def with_skills(project: Data | None = None, library: SkillLibrary = LIBRARY) -> ConfigReport:
    """What's reported of the configuration a project file holding ``project`` makes with the
    skills of ``library``."""
    given = sources(project)
    config = given.config.model_copy(update={"skill_library": library})
    return config_report(replace(given, config=config), {})


# The limits


def test_the_limits_come_first_in_the_project_files_order() -> None:
    assert tuple(report())[: len(LIMIT_KEYS)] == LIMIT_KEYS
    assert LIMIT_KEYS == (
        "fix_attempts",
        "max_checks",
        "fallback_fixes",
        "review_rounds",
        "review_slots",
        "super_review_every",
        "review_suggestions",
        "lens_findings",
        "super_review_suggestions",
        "super_review_sample",
    )


def test_a_limit_the_project_file_leaves_out_is_the_default() -> None:
    assert report()["fix_attempts"] == Setting(5, DEFAULT)


def test_a_limit_the_project_file_sets_is_the_projects() -> None:
    assert report({"max_checks": 7})["max_checks"] == Setting(7, PROJECT)


# The roles


def test_each_role_is_listed_field_by_field_in_order() -> None:
    roles = [key for key in report() if key.startswith("roles.")]
    assert roles == [f"roles.{name}.{field}" for name in BUILT_IN_ROLE_NAMES for field in ROLE_KEYS]


def test_each_role_field_comes_from_the_layer_that_set_it() -> None:
    settings = report(
        {"roles": {"reviewer": {"model": "sonnet"}}}, {"roles": {"planner": {"model": "opus"}}}
    )
    assert settings["roles.reviewer.model"] == Setting("sonnet", PROJECT)
    assert settings["roles.planner.model"] == Setting("opus", USER)
    orbit = PACKAGE_ROLES["coder"].tools.orbit
    assert settings["roles.coder.tools.orbit"] == Setting(orbit, PACKAGE)
    assert settings["roles.coder.tools.workspace"] == Setting("shell", PACKAGE)


def test_each_field_of_a_roles_tools_comes_from_the_layer_that_set_it() -> None:
    settings = report({"roles": {"reviewer": {"tools": {"workspace": "none"}}}})
    orbit = PACKAGE_ROLES["reviewer"].tools.orbit
    assert settings["roles.reviewer.tools.orbit"] == Setting(orbit, PACKAGE)
    assert settings["roles.reviewer.tools.workspace"] == Setting("none", PROJECT)
    assert settings["roles.reviewer.tools.web"] == Setting(True, PACKAGE)
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
    assert settings["roles.helper.tools.orbit"].layer == DEFAULT


@pytest.mark.parametrize(("text", "lines"), [("One.", 1), ("One.\nTwo.\n\nFour.", 4)])
def test_instructions_count_their_lines(text: str, lines: int) -> None:
    assert Instructions(text, None).lines == lines


# The triggers


def test_each_trigger_is_listed_field_by_field_in_order() -> None:
    triggers = [key for key in report() if key.startswith("triggers.")]
    assert triggers == [
        f"triggers.{name}.{field}" for name in PACKAGE_TRIGGERS for field in Trigger.model_fields
    ]


def test_each_trigger_field_comes_from_the_layer_that_set_it() -> None:
    settings = report({"triggers": {"bugs-review": {"enabled": False}}})
    assert settings["triggers.bugs-review.enabled"] == Setting(False, PROJECT)
    assert settings["triggers.bugs-review.on"] == Setting("task_landed", PACKAGE)
    assert settings["triggers.bugs-review.guards"] == Setting(("review-rounds",), PACKAGE)
    assert settings["triggers.bugs-review.every"] == Setting(None, DEFAULT)
    assert settings["triggers.super-review.when"] == Setting(
        {"origin": ("manual", "planner")}, PACKAGE
    )


def test_a_shipped_triggers_prompt_is_in_the_template_it_names() -> None:
    prompt = PACKAGE_TRIGGERS["bugs-review"].prompt
    assert prompt is not None
    expected = Setting(Instructions(prompt, "lens-review.md"), PACKAGE)
    assert report()["triggers.bugs-review.prompt"] == expected


def test_a_trigger_without_a_prompt_has_none_by_default() -> None:
    assert report()["triggers.startup-check.prompt"] == Setting(None, DEFAULT)


def test_a_shipped_prompt_naming_no_file_is_in_none() -> None:
    given = sources()
    settings = config_report(replace(given, template_files={}), {})
    assert settings["triggers.bugs-review.prompt"].value.file is None


def test_a_prompt_the_project_file_gives_as_text_is_in_no_file() -> None:
    nightly = {"on": "task_finished", "role": "coder", "prompt": "Tidy up."}
    settings = report({"triggers": {"nightly": nightly}})
    assert settings["triggers.nightly.prompt"] == Setting(Instructions("Tidy up.", None), PROJECT)
    assert settings["triggers.nightly.role"] == Setting("coder", PROJECT)
    assert settings["triggers.nightly.enabled"] == Setting(True, DEFAULT)


def test_a_prompt_the_project_file_names_a_file_for_is_in_that_file() -> None:
    written = {"triggers": {"bugs-review": {"prompt": "triggers/review.md"}}}
    read = {"triggers": {"bugs-review": {"prompt": "Review {commit}."}}}
    settings = config_report(sources(written, project_read=read), {})
    prompt = Instructions("Review {commit}.", "triggers/review.md")
    assert settings["triggers.bugs-review.prompt"] == Setting(prompt, PROJECT)


def test_super_review_every_is_the_super_reviews_every_from_the_project_file() -> None:
    settings = report({"super_review_every": 7})
    assert settings["triggers.super-review.every"] == Setting(7, PROJECT)
    assert settings["super_review_every"] == Setting(7, PROJECT)


def test_a_triggers_key_lists_only_its_fields() -> None:
    assert list(subtree(report(), "triggers.bugs-review")) == [
        f"triggers.bugs-review.{field}" for field in Trigger.model_fields
    ]


# The verification steps and setup


def test_without_steps_there_are_none_by_default() -> None:
    assert report()["verification.steps"] == Setting((), DEFAULT)


def test_no_steps_the_project_file_gives_are_the_projects() -> None:
    assert report({"verification": {"steps": []}})["verification.steps"] == Setting((), PROJECT)


def test_each_step_the_project_file_gives_is_listed_field_by_field() -> None:
    steps = [
        {"name": "Lint", "command": ["ruff", "check"]},
        {"name": "Test", "command": ["pytest"], "set": "slow"},
    ]
    triggers = {"slow": {"on": "commits", "checks": "slow"}}
    settings = report({"verification": {"steps": steps}, "triggers": triggers})
    assert {key: value for key, value in settings.items() if key.startswith("verification")} == {
        "verification.steps.1.name": Setting("Lint", PROJECT),
        "verification.steps.1.command": Setting(("ruff", "check"), PROJECT),
        "verification.steps.1.set": Setting(None, PROJECT),
        "verification.steps.2.name": Setting("Test", PROJECT),
        "verification.steps.2.command": Setting(("pytest",), PROJECT),
        "verification.steps.2.set": Setting("slow", PROJECT),
        "verification.sets": Setting({}, DEFAULT),
    }


def test_without_sets_tables_there_are_none_by_default() -> None:
    assert report()["verification.sets"] == Setting({}, DEFAULT)


def test_each_sets_table_the_project_file_gives_is_listed_field_by_field() -> None:
    steps = [
        {"name": "A", "command": ["a"], "set": "slow"},
        {"name": "B", "command": ["b"]},
        {"name": "C", "command": ["c"], "set": "fast"},
    ]
    sets = {"slow": {"holds_merges": True}, "fast": {}}
    triggers = {name: {"on": "commits", "checks": name} for name in sets}
    settings = report({"verification": {"steps": steps, "sets": sets}, "triggers": triggers})
    assert {key: value for key, value in settings.items() if ".sets" in key} == {
        "verification.sets.slow.holds_merges": Setting(True, PROJECT),
        "verification.sets.fast.holds_merges": Setting(False, DEFAULT),
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
        "triggers",
        "verification",
        "setup",
        "skills",
        "mcp",
        "skill_files",
    ]


# The MCP servers


def test_without_servers_there_are_none_by_default() -> None:
    assert report()["mcp"] == Setting({}, DEFAULT)


def test_an_empty_mcp_table_the_project_file_gives_is_the_projects() -> None:
    assert report({"mcp": {}})["mcp"] == Setting({}, PROJECT)


def test_each_server_is_listed_field_by_field_from_the_layer_that_set_it() -> None:
    project = {"mcp": {"semgrep": {"url": "http://127.0.0.1:3010/mcp", "env": {"A": "$A"}}}}
    user = {"mcp": {"semgrep": {"env": {"A": "$B"}}}}
    settings = subtree(report(project, user), "mcp.semgrep")
    assert list(settings) == [f"mcp.semgrep.{key}" for key in McpServer.model_fields]
    assert settings["mcp.semgrep.url"] == Setting("http://127.0.0.1:3010/mcp", PROJECT)
    assert settings["mcp.semgrep.env"] == Setting({"A": "$B"}, USER)
    assert settings["mcp.semgrep.idle_minutes"] == Setting(10, DEFAULT)


def test_a_roles_mcp_servers_are_reported_as_a_file_gives_them() -> None:
    mcp = ["semgrep", {"server": "github", "tools": ["get_issue"]}]
    servers = {"semgrep": {"url": "http://127.0.0.1:3010/mcp"}, "github": {"command": ["gh"]}}
    project = {"mcp": servers, "roles": {"coder": {"tools": {"mcp": mcp}}}}
    assert report(project)["roles.coder.tools.mcp"] == Setting(mcp, PROJECT)
    assert report()["roles.coder.tools.mcp"] == Setting([], DEFAULT)


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
        f"roles.reviewer.{field}" for field in ROLE_KEYS
    ]


def test_a_skills_key_gives_its_files_alone() -> None:
    assert list(subtree(with_skills(), "skill_files.python")) == [
        "skill_files.python.SKILL.md",
        "skill_files.python.tests.md",
    ]


def test_a_skill_files_key_gives_that_file_alone() -> None:
    assert list(subtree(with_skills(), "skill_files.cpp.scripts/run.sh")) == [
        "skill_files.cpp.scripts/run.sh"
    ]


@pytest.mark.parametrize(
    "key", ["skill_files.python.tests", "skill_files.python.tests.m", "skill_files.python.SKILL"]
)
def test_a_key_ending_partway_through_a_skill_files_path_is_unknown(key: str) -> None:
    with pytest.raises(UnknownKey, match=f"^No setting {re.escape(key)} "):
        subtree(with_skills(), key)


def test_a_skill_files_key_gives_only_the_file_of_its_whole_path() -> None:
    files = {"tests": skill_file("Test.\n"), "tests.md": skill_file("Test.\n")}
    library = SkillLibrary(skills={"python": skill("python", files)})
    assert list(subtree(with_skills(library=library), "skill_files.python.tests")) == [
        "skill_files.python.tests"
    ]


def test_the_skills_key_gives_the_mappings_alone() -> None:
    mappings = [{"paths": ["src"], "use": ["python"]}]
    assert list(subtree(with_skills({"skills": mappings}), "skills")) == [
        "skills.1.paths",
        "skills.1.use",
    ]


def test_a_settings_own_key_gives_it_alone() -> None:
    assert subtree(report(), "fix_attempts") == {"fix_attempts": Setting(5, DEFAULT)}


@pytest.mark.parametrize(
    ("key", "parts"),
    [
        ("roles.reviewer.model", ["roles", "reviewer", "model"]),
        ("fix_attempts", ["fix_attempts"]),
        ("skill_files", ["skill_files"]),
        ("skill_files.python", ["skill_files", "python"]),
        ("skill_files.python.tests.md", ["skill_files", "python", "tests.md"]),
        ("skill_files.cpp.scripts/run.v1.sh", ["skill_files", "cpp", "scripts/run.v1.sh"]),
    ],
)
def test_a_keys_parts_are_between_its_dots_but_a_skill_files_path_is_whole(
    key: str, parts: list[str]
) -> None:
    assert key_parts(key) == parts


@pytest.mark.parametrize("key", ["roles.nope", "roles.rev", "fix_attempts.more", ""])
def test_a_key_with_no_settings_under_it_is_unknown_naming_it(key: str) -> None:
    with pytest.raises(UnknownKey, match=f"^No setting {re.escape(key)} "):
        subtree(report(), key)


# The skill map

MAP_LIBRARY = SkillLibrary(
    skills={
        name: skill(name, {"SKILL.md": skill_file("")})
        for name in ("python", "code-style", "cpp", "review-style", "application")
    }
)
"""Five skills, given out of order, for the skill map."""

PYTHON = SkillMapping(paths=("src",), use=("python", "application"))
"""The one ``[[skills]]`` entry the skill map's configuration has."""

ANALYST = {
    "instructions": "You analyse.",
    "tools": {"orbit": ["report_progress"]},
    "lane": "analyst",
    "workspace": "snapshot",
    "conversation": True,
    "merge": False,
    "verify": False,
}
"""A project's own conversational role's table."""


def lens_cells(cell: str) -> tuple[str, ...]:
    """The per-commit lens reviewers' cells for a skill, each ``cell``."""
    return (cell,) * len(LENS_REVIEWERS)


def super_cells(cell: str) -> tuple[str, ...]:
    """The super review's lens reviewers' cells for a skill, each ``cell``."""
    return (cell,) * len(SUPER_LENS_REVIEWERS)


def mapped(project: Data | None = None) -> SkillMap:
    """The skill map of the configuration a project file holding ``project`` makes, with the
    skills of :data:`MAP_LIBRARY` and the one mapping :data:`PYTHON`."""
    config = sources(project).config
    return skill_map(config.model_copy(update={"skills": (PYTHON,), "skill_library": MAP_LIBRARY}))


def test_the_skill_map_has_a_column_per_role_in_the_configurations_order() -> None:
    skills = mapped({"roles": {"qa": {"instructions": "QA."}}})
    assert skills.roles == (*BUILT_IN_ROLE_NAMES, "qa")
    assert all(tuple(cells) == skills.roles for cells in skills.cells.values())


def test_the_skill_map_has_a_row_per_skill_of_the_library_in_name_order() -> None:
    assert list(mapped().cells) == ["application", "code-style", "cpp", "python", "review-style"]


@pytest.mark.parametrize(
    ("name", "cells"),
    [
        (
            "code-style",
            ("role", "—", "role", "role", "task", *lens_cells("role"), *super_cells("role")),
        ),
        (
            "python",
            (
                "mapped",
                "mapped",
                "task / mapped",
                "reviewed task / mapped",
                "role",
                *lens_cells("reviewed task / mapped"),
                *super_cells("reviewed task / mapped"),
            ),
        ),
        (
            "cpp",
            (
                "—",
                "—",
                "task",
                "reviewed task",
                "role",
                *lens_cells("reviewed task"),
                *super_cells("reviewed task"),
            ),
        ),
        (
            "review-style",
            (
                "—",
                "—",
                "task",
                "role",
                "task",
                *lens_cells("reviewed task"),
                "role",
                "reviewed task",
                "reviewed task",
            ),
        ),
    ],
)
def test_each_built_in_roles_cell_says_where_its_agents_get_the_skill(
    name: str, cells: tuple[str, ...]
) -> None:
    assert mapped().cells[name] == dict(zip(BUILT_IN_ROLE_NAMES, cells, strict=True))


def test_a_projects_own_task_role_gets_a_skill_from_its_task_or_the_mapping() -> None:
    cells = mapped({"roles": {"qa": {"instructions": "QA."}}}).cells
    assert (cells["python"]["qa"], cells["cpp"]["qa"]) == ("task / mapped", "task")


def test_a_projects_own_conversational_role_gets_only_the_mappings_skills() -> None:
    cells = mapped({"roles": {"analyst": ANALYST}}).cells
    assert (cells["python"]["analyst"], cells["cpp"]["analyst"]) == ("mapped", "—")


def test_a_role_that_names_a_mapped_skill_gets_it_from_its_role() -> None:
    roles = {name: {"skills": ["python"]} for name in ("planner", "reviewer", "coder")}
    python = mapped({"roles": roles}).cells["python"]
    assert [python[name] for name in roles] == ["role", "role", "role"]


def test_without_a_library_the_skill_map_has_its_roles_and_no_rows() -> None:
    config = sources().config.model_copy(update={"skills": (PYTHON,)})
    assert skill_map(config) == SkillMap(roles=BUILT_IN_ROLE_NAMES, cells={})
