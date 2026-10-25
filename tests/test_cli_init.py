"""``orbit init``: the reference config written to a workspace's ``.orbit/config.toml``, an
existing one refused, ``--update`` adding to it what it doesn't mention, ``--force`` replacing it
after keeping numbered backups, and ``--roles`` copying the built-in roles' instructions beside
it with a commented-out line pointing each role at its copy."""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from orbit.agent.domain.config import LIMIT_KEYS
from orbit.agent.domain.roles import Role
from orbit.agent.external.built_in_roles import read_built_in_roles, read_instruction_files
from orbit.agent.external.config_file import read_config
from orbit.agent.external.reference_config import reference_config
from orbit.cli import app
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
    fields = ", ".join(key for key in Role.model_fields if key != "model")
    assert result.stdout.splitlines() == [
        f"Added to {path}, commented out:",
        f"  in the top level: {', '.join(LIMIT_KEYS)}",
        f"  in [roles.coder]: {fields}",
        "  at the end: [[verification.steps]], [setup], [roles.planner], [roles.reviewer],"
        " [roles.super-reviewer], [roles.configurator], [roles.security-reviewer],"
        " [triggers.review-each-commit], [mcp.github], [mcp.semgrep],"
        " [roles.security-reviewer.tools], [models.local-coder]",
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
        "[roles.coder] is set inline or by dotted keys; add its instructions line yourself:"
        ' instructions = "roles/coder.md".',
    ]
    assert path.read_text(encoding="utf-8") == text


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
