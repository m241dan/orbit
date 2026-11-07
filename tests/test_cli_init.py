"""``orbit init``: the reference config written to a workspace's ``.orbit/config.toml``, an
existing one refused, ``--update`` adding to it what it doesn't mention, ``--force`` replacing it
after keeping numbered backups, ``--roles`` copying the built-in roles' instructions beside it
with a commented-out line pointing each role at its copy, and ``--skill`` copying orbit's skills
into ``.orbit/skills/``; and ``--user`` writing the user reference config to your own
``config.toml``, refusing an existing one unless ``--force``."""

import re
import stat
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from orbit.agent.domain.config import LIMIT_KEYS
from orbit.agent.domain.roles import ROLE_TABLES, Role
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.agent.external.config_file import read_config
from orbit.agent.external.reference_config import reference_config
from orbit.agent.external.skill_files import SKILL_DIRECTORY, skill_names, skill_paths
from orbit.agent.external.user_reference import user_reference
from orbit.cli import app
from orbit.protocol import BUILT_IN_ROLE_NAMES
from tests.git_repo import init_repo

runner = CliRunner()

REFERENCE = reference_config()

WRITTEN = "Every setting is commented out at its default; uncomment what you change."

NAMES = tuple(read_instruction_files())


def switches(*names: str) -> str:
    """What ``--roles`` says uncommenting the line does for the roles ``names`` names, whose
    headers are set."""
    return (
        f"Uncommenting a role's instructions line switches that role to its copy: {headers(names)}."
    )


def headers_too(*names: str) -> str:
    """What ``--roles`` says uncommenting the line does for the roles ``names`` names, whose
    headers are commented out."""
    return (
        "Uncommenting a role's commented-out header as well as its instructions line switches"
        f" that role to its copy: {headers(names)}."
    )


def headers(names: tuple[str, ...]) -> str:
    """The headers of the roles ``names`` names, as ``--roles`` lists them."""
    return ", ".join(f"[roles.{name}]" for name in names)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An empty workspace outside git."""
    root = tmp_path / "project"
    root.mkdir()
    return root


def init(project: Path, *args: str) -> Result:
    """``orbit init`` run on the workspace at ``project`` with ``args``."""
    return runner.invoke(app, ["init", "-C", str(project), *args])


def test_an_empty_directory_gets_the_reference(project: Path) -> None:
    result = init(project)
    assert result.exit_code == 0, result.output
    path = project / ".orbit" / "config.toml"
    assert path.read_text(encoding="utf-8") == REFERENCE
    assert result.stdout.splitlines() == [
        f"Wrote orbit's reference config to {path}.",
        WRITTEN,
    ]


def test_a_second_init_is_refused_and_leaves_the_file(project: Path) -> None:
    init(project)
    path = project / ".orbit" / "config.toml"
    path.write_text("fix_attempts = 3\n")
    result = init(project)
    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        f"{path} already exists. Use orbit init --update to add the settings it doesn't mention,"
        " or --force to replace it (the old file is kept as a backup)."
    ]
    assert result.stdout == ""
    assert path.read_text() == "fix_attempts = 3\n"


def test_force_twice_keeps_both_earlier_files_and_names_them(project: Path) -> None:
    init(project)
    path = project / ".orbit" / "config.toml"
    path.write_text("fix_attempts = 3\n")
    first = init(project, "--force")
    second = init(project, "--force")
    assert (first.exit_code, second.exit_code) == (0, 0), first.output + second.output
    backups = [path.with_name("config.toml.bak.1"), path.with_name("config.toml.bak.2")]
    assert [backup.read_text(encoding="utf-8") for backup in backups] == [
        "fix_attempts = 3\n",
        REFERENCE,
    ]
    assert path.read_text(encoding="utf-8") == REFERENCE
    assert second.stdout.splitlines() == [
        f"Kept the old file as {backups[1]} and wrote orbit's reference config to {path}.",
        WRITTEN,
    ]


def test_force_without_a_file_just_writes_the_reference(project: Path) -> None:
    result = init(project, "--force")
    assert result.exit_code == 0, result.output
    path = project / ".orbit" / "config.toml"
    assert path.read_text(encoding="utf-8") == REFERENCE
    assert result.stdout.splitlines()[0] == f"Wrote orbit's reference config to {path}."


def test_a_repositorys_subdirectory_writes_to_its_top_level(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    (repo / "src" / "pkg").mkdir(parents=True)
    result = init(repo / "src" / "pkg")
    assert result.exit_code == 0, result.output
    assert (repo / ".orbit" / "config.toml").read_text(encoding="utf-8") == REFERENCE
    assert not (repo / "src" / "pkg" / ".orbit").exists()


def test_a_missing_workspace_is_refused(tmp_path: Path) -> None:
    result = init(tmp_path / "missing")
    assert result.exit_code == 2
    assert "doesn't exist" in result.stderr
    assert not (tmp_path / "missing").exists()


def test_a_file_that_cant_be_written_exits_with_status_2(project: Path) -> None:
    (project / ".orbit").write_text("")
    result = init(project)
    assert result.exit_code == 2
    assert result.stderr.startswith("Couldn't write orbit's reference config: ")
    assert str(project / ".orbit") in result.stderr
    assert result.stdout == ""


def test_update_without_a_file_writes_the_reference_as_init_does(project: Path) -> None:
    result = init(project, "--update")
    assert result.exit_code == 0, result.output
    path = project / ".orbit" / "config.toml"
    assert path.read_text(encoding="utf-8") == REFERENCE
    assert result.stdout.splitlines() == [
        f"Wrote orbit's reference config to {path}.",
        WRITTEN,
    ]


def test_update_adds_what_the_file_doesnt_mention_and_says_what(project: Path) -> None:
    path = project / ".orbit" / "config.toml"
    path.parent.mkdir()
    path.write_text('[roles.coder]\nmodel = "opus"\n')
    result = init(project, "--update")
    assert result.exit_code == 0, result.output
    fields = ", ".join(key for key in Role.model_fields if key not in {"model", *ROLE_TABLES})
    roles = ", ".join(
        table
        for name in BUILT_IN_ROLE_NAMES
        for table in (f"[roles.{name}]", f"[roles.{name}.tools]")
        if table != "[roles.coder]"
    )
    assert result.stdout.splitlines() == [
        f"Added to {path}, commented out:",
        f"  in the top level: {', '.join(LIMIT_KEYS)}",
        f"  in [roles.coder]: {fields}",
        "  at the end: [[verification.steps]], [verification.sets.mutation], [setup],"
        f" {roles}, [roles.license-reviewer], [triggers.fit-review],"
        " [triggers.bugs-review], [triggers.tests-review], [triggers.logic-review],"
        " [triggers.security-review], [triggers.performance-review], [triggers.merge-pass],"
        " [triggers.super-merge-pass], [triggers.super-review], [triggers.super-fix-review],"
        " [triggers.configure-checks], [triggers.set-up-verification], [triggers.startup-check],"
        " [triggers.startup-fix], [[skills]], [mcp.github], [mcp.semgrep],"
        " [roles.license-reviewer.tools], [models.local-coder]",
    ]
    added = (
        r"# Added by orbit init --update on \d{4}-\d{2}-\d{2}: settings this file didn't mention\."
    )
    assert re.search(f"^{added}$", path.read_text(encoding="utf-8"), re.MULTILINE)


def test_update_with_nothing_to_add_says_so_and_leaves_the_file(project: Path) -> None:
    init(project)
    result = init(project, "--update")
    assert result.exit_code == 0, result.output
    assert result.stdout == "Nothing to add.\n"
    assert (project / ".orbit" / "config.toml").read_text(encoding="utf-8") == REFERENCE


def test_update_with_force_is_refused(project: Path) -> None:
    result = init(project, "--update", "--force")
    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        "--update keeps the file and --force replaces it, so they can't go together."
    ]
    assert not (project / ".orbit").exists()


def test_update_of_a_file_that_isnt_toml_exits_with_status_2(project: Path) -> None:
    path = project / ".orbit" / "config.toml"
    path.parent.mkdir()
    path.write_text("[roles.coder\n")
    result = init(project, "--update")
    assert result.exit_code == 2
    assert result.stderr.startswith(f"Invalid orbit configuration: {path}: ")
    assert path.read_text() == "[roles.coder\n"


def test_update_of_a_file_that_cant_be_read_exits_with_status_2(project: Path) -> None:
    (project / ".orbit").write_text("")
    result = init(project, "--update")
    assert result.exit_code == 2
    assert result.stderr.startswith("Couldn't update the config from orbit's reference config: ")
    assert result.stdout == ""


def copied(*names: str) -> list[str]:
    """What ``--roles`` says of copying the instructions of the roles ``names`` names."""
    return [f"Copied the {name}'s instructions to .orbit/roles/{name}.md." for name in names]


def written_config(project: Path, text: str) -> Path:
    """The project file of the workspace at ``project``, holding ``text``."""
    path = project / ".orbit" / "config.toml"
    path.parent.mkdir()
    path.write_text(text)
    return path


def test_roles_copy_the_instructions_and_append_each_role_table_a_file_lacks(
    project: Path,
) -> None:
    path = written_config(project, "fix_attempts = 3\n")
    result = init(project, "--roles")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        *copied(*NAMES),
        f"Added to {path}, commented out:",
        *(f'  at the end: [roles.{name}], with instructions = "roles/{name}.md"' for name in NAMES),
        headers_too(*NAMES),
    ]
    assert sorted(p.name for p in (project / ".orbit" / "roles").iterdir()) == sorted(
        f"{name}.md" for name in NAMES
    )
    assert path.read_text(encoding="utf-8").startswith("fix_attempts = 3\n\n# Added by orbit")


def test_roles_keep_a_copy_already_there_and_say_so(project: Path) -> None:
    path = written_config(project, '[roles.coder]\nmodel = "opus"\n')
    mine = project / ".orbit" / "roles" / "coder.md"
    mine.parent.mkdir()
    mine.write_text("My coder.\n")
    result = init(project, "--roles")
    assert result.exit_code == 0, result.output
    others = [name for name in NAMES if name != "coder"]
    assert result.stdout.splitlines()[: len(NAMES) + 2] == [
        *copied(*others),
        "Kept your .orbit/roles/coder.md.",
        f"Added to {path}, commented out:",
        '  in [roles.coder]: instructions = "roles/coder.md"',
    ]
    assert mine.read_text() == "My coder.\n"


def test_roles_without_a_file_write_the_reference_first(project: Path) -> None:
    result = init(project, "--roles")
    assert result.exit_code == 0, result.output
    path = project / ".orbit" / "config.toml"
    assert result.stdout.splitlines() == [
        f"Wrote orbit's reference config to {path}.",
        WRITTEN,
        *copied(*NAMES),
        f"Added to {path}, commented out:",
        *(f'  in [roles.{name}]: instructions = "roles/{name}.md"' for name in NAMES),
        headers_too(*NAMES),
    ]


def test_roles_say_a_set_table_needs_its_line_uncommented_and_the_others_their_header_too(
    project: Path,
) -> None:
    path = written_config(project, '[roles.coder]\nmodel = "opus"\n')
    result = init(project, "--roles")
    assert result.exit_code == 0, result.output
    others = tuple(name for name in NAMES if name != "coder")
    assert result.stdout.splitlines() == [
        *copied(*NAMES),
        f"Added to {path}, commented out:",
        '  in [roles.coder]: instructions = "roles/coder.md"',
        *(
            f'  at the end: [roles.{name}], with instructions = "roles/{name}.md"'
            for name in others
        ),
        switches("coder"),
        headers_too(*others),
    ]


def test_uncommenting_a_roles_header_and_line_as_said_switches_that_role_alone(
    project: Path,
) -> None:
    said = init(project, "--roles").stdout.splitlines()
    assert said[-1] == headers_too(*NAMES)
    path = project / ".orbit" / "config.toml"
    text = path.read_text(encoding="utf-8")
    line = 'instructions = "roles/coder.md"'
    path.write_text(text.replace("# [roles.coder]\n", "[roles.coder]\n").replace(f"# {line}", line))
    (project / ".orbit" / "roles" / "coder.md").write_text("My coder.\n")
    roles = read_config(str(project), project.parent / "config").roles
    built_in = read_built_in_roles()
    assert roles["coder"] == built_in["coder"].model_copy(update={"instructions": "My coder."})
    assert {name: role for name, role in roles.items() if name != "coder"} == {
        name: role for name, role in built_in.items() if name != "coder"
    }


def test_roles_name_a_role_set_inline_as_needing_its_line_added_by_hand(project: Path) -> None:
    others = "".join(
        f'\n[roles.{name}]\ninstructions = "{name}.md"\n' for name in NAMES if name != "coder"
    )
    path = written_config(project, f'[roles]\ncoder = {{ model = "opus" }}\n{others}')
    text = path.read_text(encoding="utf-8")
    result = init(project, "--roles")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        *copied(*NAMES),
        "[roles.coder] is set inline or by dotted keys, so it has no table for a line: if it's"
        ' an inline table, put instructions = "roles/coder.md" inside it; if it\'s set by dotted'
        ' keys, add coder.instructions = "roles/coder.md" beside them'
        " (roles.coder.instructions at the top level).",
    ]
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            '[roles]\ncoder = { model = "opus" }\n',
            '[roles]\ncoder = { model = "opus", instructions = "roles/coder.md" }\n',
        ),
        (
            '[roles]\ncoder.model = "opus"\n',
            '[roles]\ncoder.model = "opus"\ncoder.instructions = "roles/coder.md"\n',
        ),
        (
            'roles.coder.model = "opus"\n',
            'roles.coder.model = "opus"\nroles.coder.instructions = "roles/coder.md"\n',
        ),
    ],
)
def test_following_the_inline_advice_for_its_form_gives_a_valid_config(
    project: Path, before: str, after: str
) -> None:
    written_config(project, before)
    result = init(project, "--roles")
    assert "[roles.coder] is set inline or by dotted keys" in result.stdout
    assert tomllib.loads(after)["roles"]["coder"]["instructions"] == "roles/coder.md"


def test_roles_again_keep_every_copy_and_add_nothing(project: Path) -> None:
    init(project, "--roles")
    path = project / ".orbit" / "config.toml"
    text = path.read_text(encoding="utf-8")
    result = init(project, "--roles")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        *(f"Kept your .orbit/roles/{name}.md." for name in NAMES),
        "Each role's table already has an instructions line.",
    ]
    assert path.read_text(encoding="utf-8") == text


def test_roles_with_update_do_both(project: Path) -> None:
    path = written_config(project, "fix_attempts = 3\n")
    result = init(project, "--update", "--roles")
    assert result.exit_code == 0, result.output
    said = result.stdout.splitlines()
    assert said[0] == f"Added to {path}, commented out:"
    assert said[-len(NAMES) - 1 :] == [
        *(f'  in [roles.{name}]: instructions = "roles/{name}.md"' for name in NAMES),
        headers_too(*NAMES),
    ]
    text = path.read_text(encoding="utf-8")
    assert text.count("# Added by orbit init --update on ") == 1
    assert "# [setup]" in text


def test_roles_with_force_are_refused(project: Path) -> None:
    result = init(project, "--roles", "--force")
    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        "--roles keeps the file and --force replaces it, so they can't go together."
    ]
    assert not (project / ".orbit").exists()


def test_roles_with_a_file_that_isnt_toml_exit_with_status_2(project: Path) -> None:
    path = written_config(project, "[roles.coder\n")
    result = init(project, "--roles")
    assert result.exit_code == 2
    assert result.stderr.startswith(f"Invalid orbit configuration: {path}: ")
    assert not (project / ".orbit" / "roles").exists()


def test_roles_that_cant_be_copied_exit_with_status_2(project: Path) -> None:
    written_config(project, "fix_attempts = 3\n")
    (project / ".orbit" / "roles").write_text("")
    result = init(project, "--roles")
    assert result.exit_code == 2
    assert result.stderr.startswith("Couldn't copy the built-in roles' instructions: ")
    assert result.stdout == ""


def test_roles_after_an_update_say_what_the_update_did(project: Path) -> None:
    written_config(project, "fix_attempts = 3\n")
    (project / ".orbit" / "roles").write_text("")
    result = init(project, "--update", "--roles")
    assert result.exit_code == 2
    assert result.stdout.startswith(f"Added to {project / '.orbit' / 'config.toml'}")
    assert result.stderr.startswith("Couldn't copy the built-in roles' instructions: ")


def shipped_skill(name: str) -> dict[str, str]:
    """The text of each file of the skill ``name`` orbit ships, at any depth, by its path in the
    folder."""
    entries = skill_paths(SKILL_DIRECTORY / name)
    return {path: entry.read_bytes().decode("utf-8") for path, entry in entries}


def skill_copy(project: Path, name: str) -> dict[str, str]:
    """The text of each file of the workspace at ``project``'s copy of the skill ``name``, at any
    depth, by its path in the folder."""
    folder = project / ".orbit" / "skills" / name
    files = (path for path in folder.rglob("*") if path.is_file())
    return {path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8") for path in files}


def test_skill_copies_the_skills_files_and_writes_no_config(project: Path) -> None:
    result = init(project, "--skill", "python")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["Copied python's files to .orbit/skills/python/."]
    assert skill_copy(project, "python") == shipped_skill("python")
    assert not (project / ".orbit" / "config.toml").exists()


def test_skill_keeps_a_file_already_there_and_says_so(project: Path) -> None:
    mine = project / ".orbit" / "skills" / "python" / "tests.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("# Our tests\n")
    result = init(project, "--skill", "python")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Copied python's files to .orbit/skills/python/.",
        "Kept your .orbit/skills/python/tests.md.",
    ]
    assert mine.read_text() == "# Our tests\n"


def test_skill_again_says_only_which_files_it_kept(project: Path) -> None:
    init(project, "--skill", "python")
    result = init(project, "--skill", "python")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"Kept your .orbit/skills/python/{path}." for path in sorted(shipped_skill("python"))
    ]


def test_skills_named_twice_are_copied_in_that_order(project: Path) -> None:
    mine = project / ".orbit" / "skills" / "python" / "tests.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("# Our tests\n")
    result = init(project, "--skill", "python", "--skill", "cpp")
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Copied python's files to .orbit/skills/python/.",
        "Kept your .orbit/skills/python/tests.md.",
        "Copied cpp's files to .orbit/skills/cpp/.",
    ]
    assert skill_copy(project, "cpp") == shipped_skill("cpp")


def test_an_unknown_skill_exits_with_status_2_naming_every_skill_and_copies_none(
    project: Path,
) -> None:
    result = init(project, "--skill", "pyton", "--skill", "python")
    assert result.exit_code == 2
    names = ", ".join(skill_names())
    assert len(names.split(", ")) == 18
    assert result.stderr.splitlines() == [f"No skill named pyton; orbit's skills are {names}."]
    assert result.stdout == ""
    assert not (project / ".orbit").exists()


def test_skill_with_force_is_refused(project: Path) -> None:
    result = init(project, "--skill", "python", "--force")
    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        "--skill keeps the file and --force replaces it, so they can't go together."
    ]
    assert not (project / ".orbit").exists()


def test_skill_after_an_update_does_both(project: Path) -> None:
    path = written_config(project, "fix_attempts = 3\n")
    result = init(project, "--update", "--skill", "python")
    assert result.exit_code == 0, result.output
    said = result.stdout.splitlines()
    assert said[0] == f"Added to {path}, commented out:"
    assert said[-1] == "Copied python's files to .orbit/skills/python/."
    assert "# Added by orbit init --update on " in path.read_text(encoding="utf-8")
    assert skill_copy(project, "python") == shipped_skill("python")


def test_skill_after_roles_does_both(project: Path) -> None:
    written_config(project, "fix_attempts = 3\n")
    result = init(project, "--roles", "--skill", "python")
    assert result.exit_code == 0, result.output
    said = result.stdout.splitlines()
    assert said[0] == copied(NAMES[0])[0]
    assert said[-1] == "Copied python's files to .orbit/skills/python/."


def test_skills_that_cant_be_copied_exit_with_status_2(project: Path) -> None:
    (project / ".orbit").mkdir()
    (project / ".orbit" / "skills").write_text("")
    result = init(project, "--skill", "python")
    assert result.exit_code == 2
    assert result.stderr.startswith("Couldn't copy orbit's skills: ")
    assert result.stdout == ""


def test_the_written_config_loads_and_a_tools_table_uncommented_changes_only_its_field(
    project: Path,
) -> None:
    init(project)
    path = project / ".orbit" / "config.toml"
    config_dir = project.parent / "config"
    assert read_config(str(project), config_dir).roles == read_built_in_roles()
    before, header, after = path.read_text(encoding="utf-8").partition("# [roles.reviewer.tools]\n")
    level = after.replace('# workspace = "read"', 'workspace = "none"', 1)
    path.write_text(f"{before}{header.removeprefix('# ')}{level}", encoding="utf-8")
    reviewer = read_config(str(project), config_dir).roles["reviewer"]
    shipped = read_built_in_roles()["reviewer"]
    assert reviewer == shipped.model_copy(
        update={"tools": shipped.tools.model_copy(update={"workspace": "none"})}
    )


USER_REFERENCE = user_reference()


def init_user(*args: str, env: dict[str, str] | None = None) -> Result:
    """``orbit init --user`` run with ``args``, in ``env``'s environment over the current."""
    return runner.invoke(app, ["init", "--user", *args], env=env)


def test_user_writes_the_user_reference_in_a_new_private_config_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    result = init_user("--config-dir", str(config_dir))
    assert result.exit_code == 0, result.output
    path = config_dir / "config.toml"
    assert path.read_text(encoding="utf-8") == USER_REFERENCE
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert result.stdout.splitlines() == [
        f"Wrote orbit's user reference config to {path}.",
        WRITTEN,
    ]


def test_user_writes_in_the_config_dir_the_environment_names(tmp_path: Path) -> None:
    result = init_user(env={"ORBIT_CONFIG_DIR": str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == USER_REFERENCE


def test_user_refuses_an_existing_file_and_leaves_it(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 8000\n")
    result = init_user("--config-dir", str(tmp_path))
    assert result.exit_code == 2
    assert result.stderr.strip() == (
        f"{path} already exists. Use orbit init --user --update to add the settings it doesn't"
        " mention, or --force to replace it (the old file is kept as a backup)."
    )
    assert path.read_text() == "[server]\nport = 8000\n"
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_user_force_keeps_the_old_file_as_a_backup_and_writes_the_reference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 8000\n")
    result = init_user("--force", "--config-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    backup = tmp_path / "config.toml.bak.1"
    assert backup.read_text() == "[server]\nport = 8000\n"
    assert path.read_text(encoding="utf-8") == USER_REFERENCE
    assert result.stdout.splitlines()[0] == (
        f"Kept the old file as {backup} and wrote orbit's user reference config to {path}."
    )


@pytest.mark.parametrize(
    ("args", "option"),
    [(("--roles",), "--roles"), (("--skill", "python"), "--skill"), (("-C", "."), "-C")],
)
def test_user_refuses_the_project_options(
    tmp_path: Path, args: tuple[str, ...], option: str
) -> None:
    result = init_user(*args, "--config-dir", str(tmp_path / "config"))
    assert result.exit_code == 2
    assert result.stderr.strip() == (
        f"--user writes your own config.toml, so it can't go with {option}."
    )
    assert not (tmp_path / "config").exists()


def test_user_update_is_not_available_yet(tmp_path: Path) -> None:
    result = init_user("--update", "--config-dir", str(tmp_path / "config"))
    assert result.exit_code == 2
    assert result.stderr.strip() == "--user --update isn't available yet."
    assert not (tmp_path / "config").exists()


def test_config_dir_without_user_is_refused(project: Path, tmp_path: Path) -> None:
    result = init(project, "--config-dir", str(tmp_path / "config"))
    assert result.exit_code == 2
    assert result.stderr.strip() == (
        "--config-dir names your own config.toml's directory, so it goes with --user."
    )
    assert not (project / ".orbit").exists()
    assert not (tmp_path / "config").exists()


def test_user_with_an_unwritable_config_dir_exits_with_status_2(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.write_text("")
    result = init_user("--config-dir", str(config_dir))
    assert result.exit_code == 2
    assert result.stderr.startswith("Couldn't write orbit's user reference config: ")
    assert result.stdout == ""
