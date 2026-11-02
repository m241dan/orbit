import json
import os
import re
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from orbit.agent.application.ports import ConfigWriteFailed
from orbit.agent.domain import review_sample
from orbit.agent.domain.checks import ChecksProposal, Setup, VerificationSet
from orbit.agent.domain.config import SUPER_REVIEW_EVERY_MOVING, OrbitConfig
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_report import Written
from orbit.agent.domain.config_update import (
    Additions,
    ConfigUpdate,
    reference_with_checks,
    update_text,
)
from orbit.agent.domain.layers import PACKAGE, Layer
from orbit.agent.domain.runtimes import CLAUDE, KIRO, RUNTIME_NAMES, resolve_model
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.triggers import ChecksTrigger
from orbit.agent.domain.verification import CheckSet, VerificationStep
from orbit.agent.external.built_in_roles import read_built_in_roles
from orbit.agent.external.built_in_triggers import read_built_in_triggers
from orbit.agent.external.claude_cli import ClaudeCliRunner
from orbit.agent.external.config_file import (
    CONFIG_PATH,
    REFERENCE,
    read_config,
    read_config_sources,
    write_checks,
)
from orbit.agent.external.kiro_cli import KiroCliRunner
from orbit.agent.external.role_instructions import resolve_instructions
from orbit.agent.external.runtimes import (
    FULL_MODEL_IDS,
    RUNTIME_CAPABILITIES,
    read_package_models,
    read_runtime_rules,
)
from orbit.agent.external.skill_files import PROJECT_SKILLS, SKILL_DIRECTORY, read_skill_library
from orbit.protocol import BUILT_IN_ROLE_NAMES, LENS_REVIEWERS, PLANNER, SUPER_LENS_REVIEWERS
from tests.agent_fakes import CLAUDE_MODELS, KIRO_MODELS, LIMITED_RUNTIME

BUILT_IN_ROLES = read_built_in_roles()

MODELS = {CLAUDE: CLAUDE_MODELS, KIRO: KIRO_MODELS}
"""The models each runtime takes without a user config."""

SKILLS = read_skill_library(str(Path(__file__).parent))
"""The skills in effect in a workspace without skills of its own: the package's."""

BUILT_IN_TRIGGERS = read_built_in_triggers()
"""The triggers shipped with orbit, their prompts their templates' text."""

DEFAULTS = OrbitConfig(
    roles=BUILT_IN_ROLES, triggers=BUILT_IN_TRIGGERS, models=MODELS, skill_library=SKILLS
)
"""The configuration in effect without a project file or a user config."""

SHIPPED_SKILLS = {
    "code-style",
    "python",
    "cpp",
    "application",
    "pure-library",
    "io-library",
    "execution-library",
    "library-api",
    "security",
    "review-fit",
    "review-bugs",
    "review-tests",
    "review-logic",
    "review-security",
    "review-performance",
    "review-style",
    "review-drift",
    "review-scaling",
}
"""The skills orbit ships."""

CLAUDES_ALIASES = (
    "the runtime claude takes the aliases fable, opus, sonnet, haiku or a full id starting with"
    ' "claude-"'
)
"""What says which models Claude Code takes, as orbit ships its aliases."""


def write(root: Path, text: str) -> Path:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_without_a_file_the_defaults_apply(tmp_path: Path) -> None:
    assert read_config(str(tmp_path), tmp_path / "config") == DEFAULTS


def test_without_a_file_the_skills_are_the_18_orbit_ships(tmp_path: Path) -> None:
    skills = read_config(str(tmp_path), tmp_path / "config").skill_library.skills
    assert set(skills) == SHIPPED_SKILLS
    layers = {file.layer for skill in skills.values() for file in skill.files.values()}
    assert layers == {PACKAGE}


def test_a_shipped_skill_has_its_front_matter_its_body_and_its_files(tmp_path: Path) -> None:
    code_style = read_config(str(tmp_path), tmp_path / "config").skill_library.skills["code-style"]
    skill_md = (SKILL_DIRECTORY / "code-style" / "SKILL.md").read_text(encoding="utf-8")
    assert f"\ndescription: {code_style.description}\n" in skill_md
    assert code_style.body.startswith("# Code style\n")
    assert set(code_style.files) == {
        "SKILL.md",
        "dependencies.md",
        "functional.md",
        "naming.md",
        "performance.md",
        "state.md",
        "telemetry.md",
        "testing.md",
        "types.md",
        "writing.md",
    }
    assert code_style.files["SKILL.md"].text == skill_md


def test_a_project_file_cant_set_the_skills(tmp_path: Path) -> None:
    path = write(tmp_path, "[skill_library]\nskills = {}\n")
    unknown = f"^{re.escape(str(path))}: skill_library: Extra inputs are not permitted"
    with pytest.raises(ConfigError, match=unknown):
        read_config(str(tmp_path), tmp_path / "config")


OUTSIDE = "skills.1.paths: paths are relative to the project root and stay inside it."


def write_skills(root: Path, *entries: tuple[list[str], list[str]]) -> Path:
    """The project file in ``root`` with a ``[[skills]]`` entry of each of ``entries``, its
    paths and skills, with ``src`` a folder and ``noxfile.py`` a file in the project."""
    (root / "src").mkdir()
    (root / "noxfile.py").write_text("")
    tables = (
        f"[[skills]]\npaths = {json.dumps(paths)}\nuse = {json.dumps(use)}\n"
        for paths, use in entries
    )
    return write(root, "\n".join(tables))


def mapping_refused(root: Path, path: Path, problem: str) -> None:
    """Assert reading the configuration in ``root`` raises ``problem``, naming the project file
    at ``path``."""
    with pytest.raises(ConfigError, match=f"^{re.escape(f'{path}: {problem}')}$"):
        read_config(str(root), root / "config")


def test_skills_for_a_folder_and_a_file_in_the_project_are_read(tmp_path: Path) -> None:
    write_skills(tmp_path, (["src", "noxfile.py"], ["python"]))
    skills = read_config(str(tmp_path), tmp_path / "config").skills
    assert skills == (SkillMapping(paths=("src", "noxfile.py"), use=("python",)),)


def test_a_path_through_dot_dot_inside_the_project_is_read(tmp_path: Path) -> None:
    write_skills(tmp_path, (["src/../src"], ["python"]))
    skills = read_config(str(tmp_path), tmp_path / "config").skills
    assert skills == (SkillMapping(paths=("src/../src",), use=("python",)),)


@pytest.mark.parametrize("given", ["/etc", "../other", ""])
def test_a_path_that_isnt_relative_or_leaves_the_project_names_the_file(
    tmp_path: Path, given: str
) -> None:
    path = write_skills(tmp_path, ([given], ["python"]))
    mapping_refused(tmp_path, path, OUTSIDE)


def test_a_path_holding_a_nul_byte_names_the_file(tmp_path: Path) -> None:
    path = write_skills(tmp_path, (["src\x00"], ["python"]))
    mapping_refused(tmp_path, path, OUTSIDE)


def test_a_later_path_holding_a_nul_byte_names_its_entry(tmp_path: Path) -> None:
    path = write_skills(tmp_path, (["src"], ["python"]), (["noxfile.py", "src\x00"], ["python"]))
    problem = "skills.2.paths: paths are relative to the project root and stay inside it."
    mapping_refused(tmp_path, path, problem)


def project_linking_out(tmp_path: Path) -> Path:
    """A project in ``tmp_path`` whose ``out`` links to a folder outside it, holding ``sub``."""
    root = tmp_path / "project"
    outside = tmp_path / "elsewhere"
    (outside / "sub").mkdir(parents=True)
    root.mkdir()
    (root / "out").symlink_to(outside, target_is_directory=True)
    return root


@pytest.mark.parametrize("given", ["out", "out/sub"])
def test_a_path_through_a_link_out_of_the_project_names_the_file(
    tmp_path: Path, given: str
) -> None:
    root = project_linking_out(tmp_path)
    path = write_skills(root, ([given], ["python"]))
    mapping_refused(root, path, OUTSIDE)


@pytest.mark.parametrize(
    ("paths", "first"),
    [
        (["lib", "out"], "skills.1.paths names lib, which isn't in the project."),
        (["out", "lib"], OUTSIDE),
    ],
)
def test_a_link_out_and_a_missing_path_are_refused_in_order(
    tmp_path: Path, paths: list[str], first: str
) -> None:
    root = project_linking_out(tmp_path)
    path = write_skills(root, (paths, ["python"]))
    mapping_refused(root, path, first)


def test_a_link_to_a_folder_inside_the_project_is_read(tmp_path: Path) -> None:
    write_skills(tmp_path, (["lib"], ["python"]))
    (tmp_path / "lib").symlink_to("src", target_is_directory=True)
    skills = read_config(str(tmp_path), tmp_path / "config").skills
    assert skills == (SkillMapping(paths=("lib",), use=("python",)),)


def test_a_project_reached_through_a_link_has_its_paths_read(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    write_skills(root, (["src"], ["python"]))
    (tmp_path / "link").symlink_to(root, target_is_directory=True)
    skills = read_config(str(tmp_path / "link"), tmp_path / "config").skills
    assert skills == (SkillMapping(paths=("src",), use=("python",)),)


def test_a_path_not_in_the_project_names_the_file_the_entry_and_the_path(
    tmp_path: Path,
) -> None:
    path = write_skills(tmp_path, (["src"], ["python"]), (["noxfile.py", "src/orbt"], ["python"]))
    mapping_refused(tmp_path, path, "skills.2.paths names src/orbt, which isn't in the project.")


def test_a_skill_that_isnt_one_names_the_file_and_lists_the_skills(tmp_path: Path) -> None:
    path = write_skills(tmp_path, (["src"], ["pyton"]))
    names = ", ".join(sorted(SHIPPED_SKILLS))
    problem = f"skills.1.use names pyton, which isn't a skill; the skills are {names}."
    mapping_refused(tmp_path, path, problem)


def test_a_project_skill_can_be_mapped_by_its_folders_name(tmp_path: Path) -> None:
    write_skills(tmp_path, (["src"], ["house-rules"]))
    folder = tmp_path / ".orbit" / "skills" / "house-rules"
    folder.mkdir(parents=True)
    rules = "---\nname: house-rules\ndescription: Our rules.\n---\n\n# Rules\n"
    (folder / "SKILL.md").write_text(rules)
    skills = read_config(str(tmp_path), tmp_path / "config").skills
    assert skills == (SkillMapping(paths=("src",), use=("house-rules",)),)


def test_the_file_is_read_over_the_defaults(tmp_path: Path) -> None:
    write(
        tmp_path,
        """
fix_attempts = 3

[roles.coder]
model = "sonnet"
timeout_minutes = 45
""",
    )
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.roles["reviewer"] == BUILT_IN_ROLES["reviewer"]


def write_user_config(config_dir: Path, text: str) -> Path:
    path = config_dir / "config.toml"
    path.parent.mkdir()
    path.write_text(text)
    return path


def test_the_user_configs_role_fields_are_read_beneath_the_files(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles]\neffort = "low"\n\n[roles.planner]\nmodel = "opus"\n')
    planner = read_config(str(tmp_path), config_dir).roles[PLANNER]
    assert (planner.model, planner.effort) == ("opus", "low")
    write(tmp_path, '[roles.planner]\nmodel = "sonnet"\n')
    planner = read_config(str(tmp_path), config_dir).roles[PLANNER]
    assert (planner.model, planner.effort) == ("sonnet", "low")


def test_a_runs_runtime_is_every_roles_but_those_naming_their_own(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles]\nruntime = "claude"\n')
    write(tmp_path, '[roles.coder]\nruntime = "claude"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir, runtime="nope")
    named = re.findall(r"roles\.([a-z-]+)\.runtime = \"nope\"", str(raised.value))
    assert named == [name for name in BUILT_IN_ROLE_NAMES if name != "coder"]


def test_without_a_runs_runtime_the_roles_keep_theirs(tmp_path: Path, config_dir: Path) -> None:
    assert read_config(str(tmp_path), config_dir, runtime=None) == DEFAULTS


def test_roles_are_held_to_what_each_runtime_this_version_knows_says_it_can_do() -> None:
    assert tuple(RUNTIME_CAPABILITIES) == RUNTIME_NAMES
    assert RUNTIME_CAPABILITIES[CLAUDE] is ClaudeCliRunner.capabilities
    assert RUNTIME_CAPABILITIES[KIRO] is KiroCliRunner.capabilities


def test_the_package_passes_claudes_aliases_through_to_claude_code() -> None:
    aliases = {"fable": "fable", "opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}
    assert read_package_models()[CLAUDE] == aliases
    assert list(read_package_models()[CLAUDE]) == ["fable", "opus", "sonnet", "haiku"]


def test_the_package_gives_kiro_auto_and_its_sonnet_and_haiku_but_no_opus_or_fable() -> None:
    aliases = {"auto": "auto", "sonnet": "claude-sonnet-4.5", "haiku": "claude-haiku-4.5"}
    assert read_package_models()[KIRO] == aliases
    assert list(read_package_models()) == [CLAUDE, KIRO]


def test_each_runtime_this_version_knows_says_how_to_tell_its_full_model_ids() -> None:
    assert tuple(FULL_MODEL_IDS) == RUNTIME_NAMES
    assert FULL_MODEL_IDS[CLAUDE] == ClaudeCliRunner.full_model_ids
    assert ClaudeCliRunner.full_model_ids.described == 'starting with "claude-"'
    assert FULL_MODEL_IDS[KIRO] == KiroCliRunner.full_model_ids


def test_without_a_user_config_each_runtime_takes_the_packages_aliases(config_dir: Path) -> None:
    rules = read_runtime_rules(config_dir)
    assert rules.models == MODELS
    assert rules.capabilities == RUNTIME_CAPABILITIES


def test_the_user_configs_aliases_are_added_to_the_packages_or_take_their_place(
    config_dir: Path,
) -> None:
    write_user_config(
        config_dir,
        '[runtimes.claude.models]\nfast = "claude-haiku-5-5"\nopus = "claude-opus-5-5"\n',
    )
    claude = read_runtime_rules(config_dir).models[CLAUDE]
    assert claude.aliases == {
        **CLAUDE_MODELS.aliases,
        "opus": "claude-opus-5-5",
        "fast": "claude-haiku-5-5",
    }


def test_user_aliases_for_a_runtime_this_version_doesnt_know_are_refused_naming_the_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[runtimes.nope.models]\nfast = "nope-1"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f"{path}: runtimes.nope: this version of orbit knows the runtimes claude, kiro."
    )


def test_a_roles_model_may_be_an_alias_the_user_config_adds(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[runtimes.claude.models]\nfast = "claude-haiku-5-5"\n')
    write(tmp_path, '[roles.reviewer]\nmodel = "fast"\n')
    config = read_config(str(tmp_path), config_dir)
    assert config.roles["reviewer"].model == "fast"
    assert config.models[CLAUDE].aliases["fast"] == "claude-haiku-5-5"


def test_a_roles_model_its_runtime_doesnt_take_is_refused_naming_the_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.reviewer]\nmodel = "sonet"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f'{path}: roles.reviewer.model = "sonet": {CLAUDES_ALIASES}.'


def test_a_users_role_model_its_runtime_doesnt_take_is_refused_naming_the_user_config(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[roles.reviewer]\nmodel = "sonet"\n')
    write(tmp_path, "")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f'{path}: roles.reviewer.model = "sonet": {CLAUDES_ALIASES}.'


def test_a_users_all_roles_model_its_runtime_doesnt_take_is_refused_once_as_written(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[roles]\nmodel = "sonet"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f'{path}: roles.model = "sonet": {CLAUDES_ALIASES}.'


def test_a_users_role_model_without_a_project_file_doesnt_name_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles.coder]\nmodel = "sonet"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(CONFIG_PATH) not in str(raised.value)


def test_a_projects_bad_model_is_reported_before_the_users(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles.coder]\nmodel = "sonet"\n')
    path = write(tmp_path, '[roles.reviewer]\nmodel = "opu"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f'{path}: roles.reviewer.model = "opu": {CLAUDES_ALIASES}.'


def test_a_role_runs_on_kiro_with_no_model_of_its_own(tmp_path: Path, config_dir: Path) -> None:
    write(tmp_path, '[roles.coder]\nruntime = "kiro"\n')
    coder = read_config(str(tmp_path), config_dir).roles["coder"]
    assert (coder.runtime, coder.model) == (KIRO, None)


def test_a_role_on_kiro_may_take_a_full_id(tmp_path: Path, config_dir: Path) -> None:
    write(tmp_path, '[roles.coder]\nruntime = "kiro"\nmodel = "glm-5"\n')
    config = read_config(str(tmp_path), config_dir)
    assert config.roles["coder"].model == "glm-5"
    assert resolve_model(config.models[KIRO], "glm-5") == "glm-5"


def test_opus_on_kiro_is_refused_listing_kiros_aliases(tmp_path: Path, config_dir: Path) -> None:
    path = write(tmp_path, '[roles.coder]\nruntime = "kiro"\nmodel = "opus"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f'{path}: roles.coder.model = "opus": the runtime kiro takes the aliases auto, sonnet,'
        " haiku or a full id of lowercase letters, digits, dots and dashes, with at least one"
        " dash."
    )


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_asking_for_what_its_runtime_cant_do_is_refused_naming_the_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.coder]\nruntime = "lite"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f'{path}: roles.coder asks for tool_access "full", but its runtime lite accepts only'
        ' tool_access "restricted".'
    )


KIROS_OPUS = (
    'roles.style-reviewer.model = "opus": the runtime kiro takes the aliases auto, sonnet,'
    " haiku or a full id of lowercase letters, digits, dots and dashes, with at least one dash."
)


def test_a_users_runtime_refusing_a_package_model_names_the_user_config(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[roles]\nruntime = "kiro"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f"{path}: {KIROS_OPUS}")
    assert str(CONFIG_PATH) not in str(raised.value)


def test_a_projects_runtime_refusing_a_package_model_names_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.style-reviewer]\nruntime = "kiro"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f"{path}: {KIROS_OPUS}"


def test_a_project_file_setting_the_super_reviewer_fails_to_load_saying_what_it_became(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.super-reviewer]\nmodel = "sonnet"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f"{path}: super-reviewer is now style-reviewer, drift-reviewer and scaling-reviewer."
    )


def test_a_runs_runtime_refusing_a_package_model_names_the_user_config_and_the_runtime(
    tmp_path: Path, config_dir: Path
) -> None:
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir, "kiro")
    assert str(raised.value).startswith(
        f"{config_dir / 'config.toml'} with --runtime kiro: {KIROS_OPUS}"
    )
    assert str(CONFIG_PATH) not in str(raised.value)


def test_a_refused_model_of_a_role_naming_its_own_runtime_leaves_the_runs_runtime_out(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[roles.coder]\nruntime = "claude"\nmodel = "sonet"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir, "claude")
    assert str(raised.value).startswith(f'{path}: roles.coder.model = "sonet"')


def test_a_personal_role_breaking_a_rule_leaves_the_runs_runtime_out(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_helper(config_dir, HELPER.replace('"snapshot"', '"worktree"'))
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir, "claude")
    assert str(raised.value).startswith(f"{path}: roles: the enabled roles")


@pytest.mark.usefixtures("limited_runtime")
def test_a_users_runtime_refusing_what_a_role_asks_names_the_user_config(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[roles.coder]\nruntime = "lite"\n')
    write(tmp_path, "")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f'{path}: roles.coder asks for tool_access "full", but its runtime lite accepts only'
        ' tool_access "restricted".'
    )


@pytest.mark.usefixtures("limited_runtime")
def test_a_users_runtime_refusing_what_the_project_asks_names_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles.coder]\nruntime = "lite"\n')
    path = write(tmp_path, '[roles.coder]\ntool_access = "full"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f"{path}: roles.coder asks for")


@pytest.mark.usefixtures("limited_runtime")
def test_a_role_on_a_runtime_without_usage_is_read(tmp_path: Path, config_dir: Path) -> None:
    write(tmp_path, '[roles.reviewer]\nruntime = "lite"\n')
    assert read_config(str(tmp_path), config_dir).roles["reviewer"].runtime == LIMITED_RUNTIME


def test_a_user_config_without_roles_leaves_them_as_they_are(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, "[storage]\ntool_output_kb = 1\n")
    assert read_config(str(tmp_path), config_dir) == DEFAULTS


def test_a_role_field_the_user_config_cant_set_names_it_and_both_files(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, "[roles.coder]\nverify = false\n")
    write(tmp_path, "fix_attempts = 2\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(
        f"{path}: roles.coder.verify: belongs in the project's .orbit/config.toml"
    )


HELPER = """
[roles.helper]
instructions = "roles/helper.md"
lane = "helper"
workspace = "snapshot"
merge = false
verify = false
"""
"""A personal role whose instructions are in a file under the config directory."""


def write_helper(config_dir: Path, text: str = HELPER) -> Path:
    """The user config in ``config_dir`` defining the personal role ``helper`` as ``text``
    says, with its instructions file."""
    path = write_user_config(config_dir, text)
    (config_dir / "roles").mkdir()
    (config_dir / "roles" / "helper.md").write_text("You help.\n", encoding="utf-8")
    return path


def test_a_personal_role_is_read_into_every_workspace_marked_personal(
    tmp_path: Path, config_dir: Path
) -> None:
    write_helper(config_dir)
    bare, configured = tmp_path / "bare", tmp_path / "configured"
    bare.mkdir()
    configured.mkdir()
    write(configured, "fix_attempts = 2\n")
    for root in (bare, configured):
        config = read_config(str(root), config_dir)
        helper = config.roles["helper"]
        assert (helper.instructions, helper.lane, helper.workspace) == (
            "You help.",
            "helper",
            "snapshot",
        )
        assert (helper.merge, helper.verify) == (False, False)
        assert config.personal == {"helper"}
        assert config.notices == ()


def test_a_project_role_of_a_personal_roles_name_replaces_it_with_a_notice(
    tmp_path: Path, config_dir: Path
) -> None:
    write_helper(config_dir)
    write(tmp_path, '[roles.helper]\ninstructions = "The project\'s helper."\n')
    config = read_config(str(tmp_path), config_dir)
    assert config.roles["helper"].instructions == "The project's helper."
    assert config.roles["helper"].lane == "work"
    assert config.personal == frozenset()
    assert config.notices == (
        "The project's role helper replaces your personal role of that name.",
    )


@pytest.mark.parametrize("project", [None, "fix_attempts = 2\n"])
def test_a_personal_role_breaking_a_rule_across_the_roles_names_the_user_config(
    tmp_path: Path, config_dir: Path, project: str | None
) -> None:
    path = write_helper(config_dir, HELPER.replace('"snapshot"', '"worktree"'))
    if project is not None:
        write(tmp_path, project)
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(
        f'{path}: roles: the enabled roles with workspace = "worktree" must share one lane'
    )
    assert "lane 'helper' has ['helper']" in str(raised.value)


ROLE_NAME_RULE = "a role's name is a plain name: letters, digits, - and _"


def test_a_project_role_that_isnt_plainly_named_names_the_key_and_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles."qa.lead"]\ninstructions = "Lead."\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f"{path}: roles.qa.lead: {ROLE_NAME_RULE}"


def test_a_personal_role_that_isnt_plainly_named_names_the_key_and_the_user_config(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_helper(config_dir, HELPER.replace("[roles.helper]", '[roles."qa.lead"]'))
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f"{path}: roles.qa.lead: {ROLE_NAME_RULE}"


def test_the_projects_own_clash_beside_a_personal_role_names_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    write_helper(config_dir)
    path = write(tmp_path, '[roles.auditor]\ninstructions = "Audit."\nlane = "audit"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f"{path}: roles: the enabled roles")


# Skills by role

UNKNOWN_FOR_CODER = (
    "roles.coder.skills names pyton, which isn't a skill; the skills are"
    f" {', '.join(sorted(SHIPPED_SKILLS))}."
)
"""Why the coder naming the skill ``pyton`` is refused, listing orbit's skills."""


def house_rules(root: Path) -> None:
    """The project's own skill ``house-rules`` in the workspace at ``root``."""
    folder = root / ".orbit" / "skills" / "house-rules"
    folder.mkdir(parents=True)
    rules = "---\nname: house-rules\ndescription: Our rules.\n---\n\n# Rules\n"
    (folder / "SKILL.md").write_text(rules)


def test_the_built_in_roles_skills_come_from_the_package(tmp_path: Path, config_dir: Path) -> None:
    sources = read_config_sources(str(tmp_path), config_dir)
    assert sources.config.roles["planner"].skills == ("code-style",)
    assert sources.role_layers["reviewer"]["skills"] == PACKAGE


def test_a_projects_skills_replace_a_built_in_roles_whole(tmp_path: Path, config_dir: Path) -> None:
    path = write(tmp_path, '[roles.coder]\nskills = ["code-style"]\n')
    sources = read_config_sources(str(tmp_path), config_dir)
    assert sources.config.roles["coder"].skills == ("code-style",)
    assert sources.role_layers["coder"]["skills"] == Layer("project", str(path))


def test_a_role_naming_a_skill_that_isnt_one_names_the_project_file_and_lists_the_skills(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.coder]\nskills = ["code-style", "pyton"]\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f"{path}: {UNKNOWN_FOR_CODER}"


def test_a_role_can_name_a_project_skill_by_its_folders_name(
    tmp_path: Path, config_dir: Path
) -> None:
    write(tmp_path, '[roles.coder]\nskills = ["house-rules"]\n')
    house_rules(tmp_path)
    assert read_config(str(tmp_path), config_dir).roles["coder"].skills == ("house-rules",)


def test_a_disabled_role_naming_a_skill_that_isnt_one_is_refused(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.coder]\nenabled = false\nskills = ["pyton"]\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f"{path}: {UNKNOWN_FOR_CODER}"


def test_a_project_role_naming_a_skill_that_isnt_one_names_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.auditor]\ninstructions = "Audit."\nskills = ["pyton"]\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f"{path}: roles.auditor.skills names pyton, ")


def test_a_personal_roles_skills_are_read(tmp_path: Path, config_dir: Path) -> None:
    write_helper(config_dir, f'{HELPER}skills = ["code-style", "house-rules"]\n')
    house_rules(tmp_path)
    helper = read_config(str(tmp_path), config_dir).roles["helper"]
    assert helper.skills == ("code-style", "house-rules")


def test_a_personal_role_naming_a_skill_that_isnt_one_names_the_user_config(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_helper(config_dir, f'{HELPER}skills = ["pyton"]\n')
    write(tmp_path, "fix_attempts = 2\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f"{path}: roles.helper.skills names pyton, ")


def test_the_roles_skills_are_checked_before_the_skills_by_path(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_skills(tmp_path, (["src"], ["pyton"]))
    path.write_text(f'[roles.coder]\nskills = ["pyton"]\n\n{path.read_text()}')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == f"{path}: {UNKNOWN_FOR_CODER}"


# What orbit config show reads


def test_the_sources_hold_the_files_as_written_and_where_each_role_field_came_from(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write(tmp_path, '[roles.reviewer]\nmodel = "sonnet"\ninstructions = "roles/r.md"\n')
    (path.parent / "roles").mkdir()
    (path.parent / "roles" / "r.md").write_text("Review.\n", encoding="utf-8")
    user_path = write_user_config(config_dir, '[roles.planner]\nmodel = "opus"\n')
    sources = read_config_sources(str(tmp_path), config_dir)
    project, user = Layer("project", str(path)), Layer("user", str(user_path))
    reviewer = {"model": "sonnet", "instructions": "roles/r.md"}
    assert sources.project == Written(project, {"roles": {"reviewer": reviewer}})
    assert sources.user == Written(user, {"roles": {"planner": {"model": "opus"}}})
    assert sources.config == read_config(str(tmp_path), config_dir)
    assert sources.config.roles["reviewer"].instructions == "Review."
    assert sources.role_layers["reviewer"]["model"] == project
    assert sources.role_layers["planner"]["model"] == user
    assert sources.package_files == {
        name: (
            "lens-reviewer.md"
            if name in LENS_REVIEWERS
            else "super-lens-reviewer.md"
            if name in SUPER_LENS_REVIEWERS
            else f"{name}.md"
        )
        for name in BUILT_IN_ROLE_NAMES
    }


def test_without_files_the_sources_hold_nothing_as_written(
    tmp_path: Path, config_dir: Path
) -> None:
    sources = read_config_sources(str(tmp_path), config_dir)
    assert sources.project == Written(Layer("project", str(tmp_path / CONFIG_PATH)), {})
    assert sources.user == Written(Layer("user", str(config_dir / "config.toml")), {})
    assert sources.config == DEFAULTS


def test_reading_the_sources_of_an_invalid_file_names_it(tmp_path: Path, config_dir: Path) -> None:
    path = write(tmp_path, "fix_attempts = 0\n")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: fix_attempts: "):
        read_config_sources(str(tmp_path), config_dir)


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(ConfigError, match=str(path)):
        read_config(str(tmp_path), tmp_path / "config")


def test_a_non_utf8_file_raises_config_error_naming_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"fix_attempts = 3 # \xff\n")
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_config(str(tmp_path), tmp_path / "config")


def test_a_config_path_that_is_a_directory_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_PATH
    path.mkdir(parents=True)
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        read_config(str(tmp_path), tmp_path / "config")


def test_invalid_settings_name_the_file_and_the_problem(tmp_path: Path) -> None:
    path = write(tmp_path, "[roles.coder]\ntools = ['launch_rockets']\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert str(path) in str(raised.value)
    assert "roles.coder.tools" in str(raised.value)


def test_the_file_is_orbit_config_toml(tmp_path: Path) -> None:
    assert CONFIG_PATH == ".orbit/config.toml"
    write(tmp_path, "fix_attempts = 2\n")
    assert read_config(str(tmp_path), tmp_path / "config").fix_attempts == 2


def test_the_old_roles_toml_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "roles.toml").write_text("fix_attempts = 2\n")
    assert read_config(str(tmp_path), tmp_path / "config") == DEFAULTS


def test_the_file_lists_verification_steps(tmp_path: Path) -> None:
    write(
        tmp_path,
        """
[[verification.steps]]
name = "Ruff lint"
command = ["uv", "run", "ruff", "check"]

[[verification.steps]]
name = "Tests"
command = ["uv", "run", "pytest"]
""",
    )
    assert read_config(str(tmp_path), tmp_path / "config").verification.steps == (
        VerificationStep(name="Ruff lint", command=("uv", "run", "ruff", "check")),
        VerificationStep(name="Tests", command=("uv", "run", "pytest")),
    )


def test_without_steps_in_the_file_there_are_none(tmp_path: Path) -> None:
    write(tmp_path, "fix_attempts = 2\n")
    assert read_config(str(tmp_path), tmp_path / "config").verification.steps == ()


def test_an_invalid_step_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[[verification.steps]]\nname = "Lint"\ncommand = []\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert str(path) in str(raised.value)
    assert "step 1 has no command" in str(raised.value)


def write_instructions(root: Path, name: str, text: str) -> Path:
    path = root / ".orbit" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_role_is_told_the_text_of_the_markdown_file_its_instructions_name(
    tmp_path: Path,
) -> None:
    write(tmp_path, '[roles.coder]\ninstructions = "roles/coder.md"\n')
    write_instructions(tmp_path, "roles/coder.md", "Write the code with care.\n")
    coder = read_config(str(tmp_path), tmp_path / "config").roles["coder"]
    assert coder.instructions == "Write the code with care."
    built_in = BUILT_IN_ROLES["coder"]
    assert coder == built_in.model_copy(update={"instructions": coder.instructions})


def test_inline_instructions_are_the_text_itself(tmp_path: Path) -> None:
    write(tmp_path, '[roles.reviewer]\ninstructions = "Review for security."\n')
    assert (
        read_config(str(tmp_path), tmp_path / "config").roles["reviewer"].instructions
        == "Review for security."
    )


def test_a_new_role_can_name_its_instructions_file(tmp_path: Path) -> None:
    write(tmp_path, '[roles.docs-writer]\ninstructions = "docs-writer.md"\n')
    write_instructions(tmp_path, "docs-writer.md", "You write the docs.\n")
    role = read_config(str(tmp_path), tmp_path / "config").roles["docs-writer"]
    assert role.instructions == "You write the docs."


def test_a_missing_instructions_file_names_the_file_and_the_role(tmp_path: Path) -> None:
    write(tmp_path, '[roles.coder]\ninstructions = "roles/missing.md"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert ".orbit/roles/missing.md" in str(raised.value)
    assert "roles.coder.instructions" in str(raised.value)


def test_an_empty_instructions_file_is_refused(tmp_path: Path) -> None:
    write(tmp_path, '[roles.coder]\ninstructions = "roles/coder.md"\n')
    write_instructions(tmp_path, "roles/coder.md", "\n")
    with pytest.raises(ConfigError, match=re.escape("roles.coder.instructions")):
        read_config(str(tmp_path), tmp_path / "config")


def test_an_instructions_file_no_role_names_changes_nothing(tmp_path: Path) -> None:
    write(tmp_path, "fix_attempts = 2\n")
    write_instructions(tmp_path, "roles/reviewer.md", "Approve everything.\n")
    assert read_config(str(tmp_path), tmp_path / "config").roles == BUILT_IN_ROLES


def test_without_a_file_the_triggers_are_those_orbit_ships(tmp_path: Path) -> None:
    triggers = read_config(str(tmp_path), tmp_path / "config").triggers
    assert list(triggers) == [
        "fit-review",
        "bugs-review",
        "tests-review",
        "logic-review",
        "security-review",
        "performance-review",
        "merge-pass",
        "super-review",
        "super-fix-review",
        "configure-checks",
        "set-up-verification",
        "startup-check",
        "startup-fix",
    ]
    assert triggers == BUILT_IN_TRIGGERS


def test_a_trigger_is_given_the_text_of_the_markdown_file_its_prompt_names(
    tmp_path: Path,
) -> None:
    write(tmp_path, '[triggers.bugs-review]\nprompt = "triggers/review.md"\n')
    write_instructions(tmp_path, "triggers/review.md", "Review {commit}.\n\n{diff}\n")
    review = read_config(str(tmp_path), tmp_path / "config").triggers["bugs-review"]
    assert review == BUILT_IN_TRIGGERS["bugs-review"].model_copy(
        update={"prompt": "Review {commit}.\n\n{diff}"}
    )


def test_a_missing_prompt_file_names_the_file_and_the_trigger(tmp_path: Path) -> None:
    path = write(tmp_path, '[triggers.bugs-review]\nprompt = "triggers/missing.md"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), tmp_path / "config")
    assert str(raised.value).startswith(f"{path}: triggers.bugs-review.prompt: ")
    assert ".orbit/triggers/missing.md" in str(raised.value)


def test_an_invalid_trigger_names_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, '[triggers.nightly]\non = "task_finished"\nrole = "tidier"\n')
    with pytest.raises(ConfigError, match=f"^{re.escape(str(path))}: triggers.nightly.role: "):
        read_config(str(tmp_path), tmp_path / "config")


def test_triggers_in_the_user_config_name_it(tmp_path: Path, config_dir: Path) -> None:
    path = write_user_config(config_dir, '[triggers.nightly]\non = "task_finished"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f"{path}: triggers: belongs in the project's .orbit/config.toml; the user config sets"
        " only a role's model, effort and runtime"
    )


def test_super_review_every_in_the_file_is_the_super_reviews_every_with_a_notice(
    tmp_path: Path,
) -> None:
    write(tmp_path, "super_review_every = 7\n")
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.triggers["super-review"].every == 7
    assert config.notices == (SUPER_REVIEW_EVERY_MOVING,)


def test_an_instructions_file_is_ignored_without_a_config_file(tmp_path: Path) -> None:
    write_instructions(tmp_path, "roles/coder.md", "Write no tests.\n")
    assert read_config(str(tmp_path), tmp_path / "config") == DEFAULTS


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[roles]\ncoder = 3\n", "roles.coder"),
        ("[roles.coder]\ninstructions = 3\n", "roles.coder.instructions"),
        ("roles = 3\n", "roles"),
    ],
)
def test_roles_that_are_not_tables_of_text_are_left_for_validation_to_refuse(
    tmp_path: Path, text: str, problem: str
) -> None:
    write(tmp_path, text)
    with pytest.raises(ConfigError, match=re.escape(problem)):
        read_config(str(tmp_path), tmp_path / "config")


def test_resolving_a_markdown_path_reads_it_from_the_base_as_utf8(tmp_path: Path) -> None:
    (tmp_path / "coder.md").write_text("\nZoë writes ✓ code.\n\n", encoding="utf-8")
    assert resolve_instructions("coder.md", tmp_path) == "Zoë writes ✓ code."


@pytest.mark.parametrize(
    "value",
    ["Review for security.", "Read the notes in\nREADME.md", "Follow coder.md closely."],
)
def test_resolving_anything_but_one_line_ending_in_md_gives_the_text(
    tmp_path: Path, value: str
) -> None:
    assert resolve_instructions(value, tmp_path) == value


def test_resolving_a_missing_file_raises_config_error_naming_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=re.escape(str(tmp_path / "roles" / "missing.md"))):
        resolve_instructions("roles/missing.md", tmp_path)


def test_resolving_a_non_utf8_file_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "coder.md"
    path.write_bytes(b"Write \xff code.\n")
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        resolve_instructions("coder.md", tmp_path)


def test_resolving_a_directory_raises_config_error_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "roles.md"
    path.mkdir()
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        resolve_instructions("roles.md", tmp_path)


ORBITS_OWN_STEPS = [
    ("Ruff lint", ("uv", "run", "ruff", "check")),
    ("Ruff format", ("uv", "run", "ruff", "format", "--check")),
    ("Type check (mypy)", ("uv", "run", "mypy")),
    ("Import contracts", ("uv", "run", "lint-imports")),
    (
        "Code quality (Lizard)",
        ("uv", "run", "lizard", "src", "tests", "-w", "-C", "6", "-L", "100"),
    ),
    ("Unit tests with 100% coverage", ("uv", "run", "pytest", "--cov", "--cov-fail-under=100")),
]


def test_orbit_verifies_itself_with_its_six_steps_quick_ones_first(config_dir: Path) -> None:
    root = Path(__file__).parent.parent
    steps = read_config(str(root), config_dir).verification.steps
    assert [(s.name, s.command) for s in steps] == ORBITS_OWN_STEPS


def test_the_file_sets_up_the_worktree(tmp_path: Path) -> None:
    write(tmp_path, '[setup]\ncommand = ["uv", "sync", "--frozen"]\nwatch = ["uv.lock"]\n')
    setup = read_config(str(tmp_path), tmp_path / "config").setup
    assert setup == Setup(command=("uv", "sync", "--frozen"), watch=("uv.lock",))


def test_orbit_syncs_its_worktree_whenever_uv_lock_changes(config_dir: Path) -> None:
    root = Path(__file__).parent.parent
    setup = read_config(str(root), config_dir).setup
    assert setup == Setup(command=("uv", "sync", "--frozen"), watch=("uv.lock",))


ORBITS_OWN_SKILLS = ("application", "python", "layers", "orbit")
"""The skills orbit's own code is mapped to, its own two after the shipped ones."""


def test_orbit_maps_its_skills_to_its_code_and_tests(config_dir: Path) -> None:
    root = Path(__file__).parent.parent
    skills = read_config(str(root), config_dir).skills
    assert skills == (SkillMapping(paths=("src", "tests"), use=ORBITS_OWN_SKILLS),)


@pytest.mark.parametrize("name", ["layers", "orbit"])
def test_orbits_own_skills_come_from_the_project(config_dir: Path, name: str) -> None:
    root = Path(__file__).parent.parent
    library = read_config(str(root), config_dir).skill_library
    path = root / PROJECT_SKILLS / name / "SKILL.md"
    assert library.skills[name].files["SKILL.md"].layer == Layer("project", str(path))


def test_orbits_own_skill_ends_by_naming_the_module_of_the_code_extensions(
    config_dir: Path,
) -> None:
    root = Path(__file__).parent.parent
    body = read_config(str(root), config_dir).skill_library.skills["orbit"].body
    section = " ".join(body[body.rindex("\n## ") :].split())
    assert section.startswith("## Languages ")
    assert f"`CODE_EXTENSIONS` in `{review_sample.__name__}`" in section


def test_orbits_own_file_mentions_every_setting_of_the_reference() -> None:
    text = (Path(__file__).parent.parent / CONFIG_PATH).read_text(encoding="utf-8")
    reference = REFERENCE.read_text(encoding="utf-8")
    assert update_text(text, reference, date(2026, 10, 25)) == ConfigUpdate(text, Additions())


ORBITS_OWN_COMMENTS = (
    (
        "# Quick checks first, so a failure shows up in seconds rather than after the tests.\n",
        '[[verification.steps]]\nname = "Ruff lint"\n',
    ),
    (
        "# Every function, tests included: cyclomatic complexity at most 6 and at most 100 lines."
        " The\n# limit of 5 parameters is held by a test, tests/test_parameter_limit.py, rather"
        " than Lizard.\n",
        '[[verification.steps]]\nname = "Code quality (Lizard)"\n',
    ),
    (
        "# The slowest check, but a task check like the rest, so every task runs it. A check to"
        " run\n# less often would join a named set, which a trigger runs:\n"
        '# set = "<name>"\n',
        '[[verification.steps]]\nname = "Unit tests with 100% coverage"\n',
    ),
    (
        "# Bring the worktree's environment in line with the lock file whenever it changes.\n",
        "[setup]\n",
    ),
)
"""The hand-written comments of orbit's own file, each with the start of the step or setup it
explains, just below it."""


SKILLS_DESCRIPTION = (
    "# Skills for the code under some paths, given to every agent along with its role's."
)
SKILLS_EXAMPLE = f"""\
{SKILLS_DESCRIPTION} For example:
# [[skills]]
# Files or folders in the project, relative to its root; each must exist.
# paths = ["src"]
# The skills that apply there, by name.
# use = ["application", "python"]
"""
ORBITS_OWN_MAPPING = f"""\
{SKILLS_DESCRIPTION}
[[skills]]
paths = ["src", "tests"]
use = ["application", "python", "layers", "orbit"]
"""
"""Orbit's own ``[[skills]]``, in place of the reference's example of them, as its checks are."""


def test_orbits_own_file_is_the_reference_with_its_checks_skills_and_comments(
    config_dir: Path,
) -> None:
    root = Path(__file__).parent.parent
    text = (root / CONFIG_PATH).read_text(encoding="utf-8")
    for comment, explained in ORBITS_OWN_COMMENTS:
        assert f"\n{comment}{explained}" in text, f"{comment!r} isn't directly above {explained!r}"
        text = text.replace(f"\n{comment}{explained}", f"\n{explained}", 1)
    config = read_config(str(root), config_dir)
    checks = ChecksProposal(steps=config.verification.steps, setup=config.setup)
    reference = reference_with_checks(REFERENCE.read_text(encoding="utf-8"), checks)
    assert text == reference.replace(SKILLS_EXAMPLE, ORBITS_OWN_MAPPING, 1), (
        "Regenerate orbit's .orbit/config.toml from the reference with its checks and its"
        " [[skills]] in place of their examples, keeping its hand-written comments above its"
        " checks."
    )


LINT = VerificationStep(name="Lint", command=("ruff", "check"))
TESTS = VerificationStep(name="Tests", command=("pytest", "-q"))
SYNC = Setup(command=("uv", "sync"), watch=("uv.lock",))


def test_writing_checks_creates_the_file_and_its_directory(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.verification.steps == (LINT, TESTS)
    assert config.setup == SYNC


REFERENCE_TEXT = REFERENCE.read_text(encoding="utf-8")
STEPS_DESCRIPTION = (
    "# The verification steps, in order; the task checks, in no set, verify every task."
)
STEPS_EXAMPLE = f"""\
{STEPS_DESCRIPTION} For example:
# [[verification.steps]]
# What the step is called; no two steps share a name.
# name = "Mutation tests"
# The command run in the working directory; it passes if it exits 0.
# command = ["uv", "run", "mutmut", "run"]
# The named set it's in; left out, it's one of the task checks every task runs.
# set = "mutation"
"""
SETUP_DESCRIPTION = "# How the coder's worktree is set up; with none, nothing is."
SETUP_EXAMPLE = f"""\
{SETUP_DESCRIPTION} For example:
# [setup]
# The command run in a fresh worktree, and again when a watched file changes.
# command = ["uv", "sync"]
# Files, relative to the tree, whose changes run the command again.
# watch = ["uv.lock"]
"""
STEP_TABLES = """\
[[verification.steps]]
name = "Lint"
command = ["ruff", "check"]

[[verification.steps]]
name = "Tests"
command = ["pytest", "-q"]
"""
SYNC_TABLE = '[setup]\ncommand = ["uv", "sync"]\nwatch = ["uv.lock"]\n'


def test_the_references_examples_are_those_a_new_file_replaces() -> None:
    assert STEPS_EXAMPLE in REFERENCE_TEXT
    assert SETUP_EXAMPLE in REFERENCE_TEXT


def test_writing_checks_without_a_file_starts_it_from_the_reference(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    expected = REFERENCE_TEXT.replace(STEPS_EXAMPLE, f"{STEPS_DESCRIPTION}\n{STEP_TABLES}").replace(
        SETUP_EXAMPLE, f"{SETUP_DESCRIPTION}\n{SYNC_TABLE}"
    )
    assert (tmp_path / CONFIG_PATH).read_text(encoding="utf-8") == expected
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.verification.steps == (LINT, TESTS)
    assert config.setup == SYNC


def test_writing_checks_without_a_file_or_a_setup_keeps_the_references_setup_example(
    tmp_path: Path,
) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS)))
    expected = REFERENCE_TEXT.replace(STEPS_EXAMPLE, f"{STEPS_DESCRIPTION}\n{STEP_TABLES}")
    assert (tmp_path / CONFIG_PATH).read_text(encoding="utf-8") == expected
    assert read_config(str(tmp_path), tmp_path / "config").setup is None


def test_writing_checks_changes_only_the_files_steps_and_setup(tmp_path: Path) -> None:
    before = "# Ours.\nfix_attempts = 2\n\n# Quick ones first.\n"
    after_steps = "\n# Keep the environment in step with the lock file.\n"
    after_setup = '\n# The roles.\n# [roles.coder]\n# model = "opus"\n'
    steps = '[[verification.steps]]\nname = "Old"\ncommand = ["nose"]\n'
    setup = '[setup]\ncommand = ["make"]\n'
    path = write(tmp_path, f"{before}{steps}{after_steps}{setup}{after_setup}")
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    assert path.read_text() == f"{before}{STEP_TABLES}{after_steps}{SYNC_TABLE}{after_setup}"


def test_rewriting_checks_without_a_setup_removes_only_its_table(tmp_path: Path) -> None:
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS), setup=SYNC))
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT, TESTS)))
    expected = REFERENCE_TEXT.replace(STEPS_EXAMPLE, f"{STEPS_DESCRIPTION}\n{STEP_TABLES}").replace(
        SETUP_EXAMPLE, f"{SETUP_DESCRIPTION}\n"
    )
    assert (tmp_path / CONFIG_PATH).read_text(encoding="utf-8") == expected


def test_writing_checks_keeps_every_other_key_and_comment(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """# Orbit's settings.
fix_attempts = 3  # Plenty.

[roles.coder]
# Cheaper.
model = "sonnet"
""",
    )
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    text = path.read_text()
    assert text.startswith("# Orbit's settings.\nfix_attempts = 3  # Plenty.\n\n[roles.coder]\n")
    assert '# Cheaper.\nmodel = "sonnet"\n' in text
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.fix_attempts == 3
    assert config.roles["coder"].model == "sonnet"
    assert config.verification.steps == (LINT,)
    assert config.setup is None


def test_writing_checks_keeps_a_utf8_comment(tmp_path: Path) -> None:
    path = tmp_path / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text("# Réglages — orbit.\n", encoding="utf-8")
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text(encoding="utf-8").startswith("# Réglages — orbit.\n")
    assert read_config(str(tmp_path), tmp_path / "config").verification.steps == (LINT,)


def test_writing_checks_replaces_the_steps_and_setup_configured_before(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """fix_attempts = 2

# Quick ones first.
[[verification.steps]]
name = "Old lint"
command = ["flake8"]

[[verification.steps]]
name = "Old tests"
command = ["nose"]

[setup]
command = ["pip", "install", "-e", "."]
watch = ["setup.py"]

[roles.coder]
model = "sonnet"
""",
    )
    write_checks(str(tmp_path), ChecksProposal(steps=(TESTS, LINT), setup=SYNC))
    text = path.read_text()
    assert "flake8" not in text and "nose" not in text and "setup.py" not in text
    assert "# Quick ones first.\n[[verification.steps]]" in text
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.fix_attempts == 2
    assert config.roles["coder"].model == "sonnet"
    assert config.verification.steps == (TESTS, LINT)
    assert config.setup == SYNC


def test_writing_checks_without_setup_removes_the_old_one(tmp_path: Path) -> None:
    write(tmp_path, '[setup]\ncommand = ["make"]\n')
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.setup is None
    assert config.verification.steps == (LINT,)


def test_writing_checks_twice_gives_the_same_file(tmp_path: Path) -> None:
    checks = ChecksProposal(steps=(LINT, TESTS), setup=SYNC)
    write_checks(str(tmp_path), checks)
    first = (tmp_path / CONFIG_PATH).read_text()
    write_checks(str(tmp_path), checks)
    assert (tmp_path / CONFIG_PATH).read_text() == first


A = VerificationStep(name="A", command=("a",))
B = VerificationStep(name="B", command=("b",))
C = VerificationStep(name="C", command=("c",), set="mutation")
EVERY_10 = ChecksTrigger(on="commits", every=10, checks="mutation")
SET_CHECKS = ChecksProposal(
    steps=(A, B, C),
    sets={"mutation": VerificationSet(holds_merges=False)},
    triggers={"mutation-every-10": EVERY_10},
)


@pytest.mark.parametrize("before", [None, "fix_attempts = 2\n"])
def test_written_sets_and_triggers_read_back_as_proposed(
    tmp_path: Path, before: str | None
) -> None:
    if before is not None:
        write(tmp_path, before)
    write_checks(str(tmp_path), SET_CHECKS)
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.verification.steps == (A, B, C)
    assert config.verification.task_checks == (A, B)
    assert config.verification.check_sets == (CheckSet("mutation", (C,), "reports"),)
    trigger = config.triggers["mutation-every-10"]
    assert (trigger.on, trigger.every, trigger.checks) == ("commits", 10, "mutation")


def test_a_holding_set_is_written_and_the_files_other_sets_and_triggers_stay(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        """[[verification.steps]]
name = "Stress"
command = ["stress"]
set = "stress"

[verification.sets.stress]
holds_merges = true

[triggers.stress-by-hand]
on = "manual"
checks = "stress"
manual = true
label = "Stress"
""",
    )
    stress = VerificationStep(name="Stress", command=("stress",), set="stress")
    holding = ChecksProposal(
        steps=(A, C),
        sets={"mutation": VerificationSet(holds_merges=True)},
        triggers={"mutation-every-10": EVERY_10},
    )
    write_checks(str(tmp_path), holding)
    config = read_config(str(tmp_path), tmp_path / "config")
    assert config.verification.check_sets == (
        CheckSet("mutation", (C,), "holds_merges"),
        CheckSet("stress", (stress,), "holds_merges"),
    )
    assert config.triggers["stress-by-hand"].label == "Stress"
    assert config.triggers["mutation-every-10"].every == 10


def test_writing_checks_where_orbits_directory_is_a_file_raises_a_config_write_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / ".orbit").write_text("")
    with pytest.raises(
        ConfigWriteFailed, match=re.escape(f"Couldn't save the checks to {tmp_path}")
    ):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))


def test_writing_checks_into_an_unparsable_file_raises_a_config_write_failure(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, "fix_attempts = = 3\n")
    with pytest.raises(ConfigWriteFailed, match=re.escape(f"Couldn't save the checks to {path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = = 3\n"


def test_writing_checks_into_a_non_utf8_file_raises_a_config_write_failure(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"fix_attempts = 3 # \xff\n")
    with pytest.raises(ConfigWriteFailed, match=re.escape(f"Couldn't save the checks to {path}")):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))


def test_writing_checks_that_fails_leaves_the_old_file_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")

    def full(source: Path, target: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(ConfigWriteFailed, match="No space left on device"):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = 3\n"
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_writing_checks_that_fails_midway_leaves_the_old_file_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")

    def full(descriptor: int) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(ConfigWriteFailed, match="No space left on device"):
        write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert path.read_text() == "fix_attempts = 3\n"
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_writing_checks_keeps_the_files_mode(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    path.chmod(0o664)
    write_checks(str(tmp_path), ChecksProposal(steps=(LINT,)))
    assert stat.S_IMODE(path.stat().st_mode) == 0o664


def test_a_utf8_file_is_read_under_an_ascii_locale(tmp_path: Path) -> None:
    write(tmp_path, "")
    (tmp_path / CONFIG_PATH).write_text("# Zoë ✓\nfix_attempts = 4\n", encoding="utf-8")
    script = (
        "import pathlib, sys; from orbit.agent.external.config_file import read_config;"
        " assert read_config(sys.argv[1], pathlib.Path(sys.argv[2])).fix_attempts == 4"
    )
    ascii_locale = {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    env = {**os.environ, **ascii_locale}
    config_dir = str(tmp_path / "config")
    subprocess.run([sys.executable, "-c", script, str(tmp_path), config_dir], check=True, env=env)


@pytest.mark.usefixtures("limited_runtime")
def test_a_users_runtime_refusing_a_package_ask_names_the_user_config_beside_a_project_field(
    tmp_path: Path, config_dir: Path
) -> None:
    path = write_user_config(config_dir, '[roles.coder]\nruntime = "lite"\n')
    write(tmp_path, "[roles.coder]\nconversation = false\n")
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value) == (
        f'{path}: roles.coder asks for tool_access "full", but its runtime lite accepts only'
        ' tool_access "restricted".'
    )


@pytest.mark.usefixtures("limited_runtime")
def test_a_users_runtime_refusing_a_project_ask_and_a_package_ask_names_the_project_first(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles.coder]\nruntime = "lite"\n')
    path = write(tmp_path, '[roles.coder]\neffort = "high"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f'{path}: roles.coder asks for effort "high"')
    assert "tool_access" not in str(raised.value)


def test_a_users_runtime_refusing_a_project_model_names_the_project_file(
    tmp_path: Path, config_dir: Path
) -> None:
    write_user_config(config_dir, '[roles.coder]\nruntime = "kiro"\n')
    path = write(tmp_path, '[roles.coder]\nmodel = "opus"\n')
    with pytest.raises(ConfigError) as raised:
        read_config(str(tmp_path), config_dir)
    assert str(raised.value).startswith(f'{path}: roles.coder.model = "opus"')
