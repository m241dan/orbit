"""``orbit init``: the reference config written to a workspace's ``.orbit/config.toml``, an
existing one refused, and ``--force`` replacing it after keeping numbered backups."""

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from orbit.agent.external.reference_config import reference_config
from orbit.cli import app
from tests.git_repo import init_repo

runner = CliRunner()

REFERENCE = reference_config()

WRITTEN = "Every setting is commented out at its default; uncomment what you change."


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
