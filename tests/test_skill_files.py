"""The skills shipped with orbit, read from the folders in the package's skills/."""

import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE
from orbit.agent.domain.skills import SkillFile
from orbit.agent.external.skill_files import SKILL_DIRECTORY, read_skill_library


def skill_md(name: str) -> str:
    """A SKILL.md naming and describing the skill ``name``."""
    return f"---\nname: {name}\ndescription: The {name} skill.\n---\n\n# {name}\n"


def write(path: Path, text: str) -> Path:
    """``path``, made with its folders, holding ``text``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def skill_folder(directory: Path, name: str) -> Path:
    """``directory``'s folder for the skill ``name``, holding its SKILL.md."""
    write(directory / name / "SKILL.md", skill_md(name))
    return directory / name


def read(directory: Path) -> dict[str, dict[str, SkillFile]]:
    """The files of each skill read from ``directory``, by skill and path."""
    library = read_skill_library(str(directory), directory)
    return {name: dict(skill.files) for name, skill in library.skills.items()}


def test_the_package_holds_a_folder_for_each_skill() -> None:
    folders = [entry for entry in SKILL_DIRECTORY.iterdir() if entry.is_dir()]
    assert {folder.name for folder in folders} == set(read_skill_library(".").skills)


def test_each_skill_folder_given_is_a_skill_of_its_files(tmp_path: Path) -> None:
    python = skill_folder(tmp_path, "python")
    write(python / "tests.md", "# Tests\n")
    skill_folder(tmp_path, "cpp")
    library = read_skill_library(str(tmp_path), tmp_path)
    assert list(library.skills) == ["cpp", "python"]
    assert library.skills["python"].description == "The python skill."
    assert library.skills["python"].files["tests.md"] == SkillFile(
        text="# Tests\n", layer=PACKAGE, location=str(python / "tests.md")
    )


def test_a_file_in_a_subfolder_is_read_under_its_relative_path(tmp_path: Path) -> None:
    python = skill_folder(tmp_path, "python")
    write(python / "scripts" / "run.sh", "#!/bin/sh\n")
    write(python / "scripts" / "lint" / "ruff.toml", "line-length = 100\n")
    files = read(tmp_path)["python"]
    assert sorted(files) == ["SKILL.md", "scripts/lint/ruff.toml", "scripts/run.sh"]
    assert files["scripts/run.sh"].text == "#!/bin/sh\n"


def test_hidden_files_and_files_in_hidden_folders_are_left_out(tmp_path: Path) -> None:
    python = skill_folder(tmp_path, "python")
    write(python / ".notes.md", "Mine.\n")
    write(python / ".git" / "HEAD", "ref: refs/heads/main\n")
    write(python / "scripts" / ".cache" / "run.sh", "#!/bin/sh\n")
    assert list(read(tmp_path)["python"]) == ["SKILL.md"]


def test_hidden_folders_and_files_beside_the_skills_are_left_out(tmp_path: Path) -> None:
    skill_folder(tmp_path, "python")
    write(tmp_path / ".drafts" / "SKILL.md", "Not a skill.\n")
    write(tmp_path / "README.md", "The skills.\n")
    assert list(read(tmp_path)) == ["python"]


def test_a_file_that_isnt_utf8_is_refused_naming_it(tmp_path: Path) -> None:
    tests = skill_folder(tmp_path, "python") / "tests.md"
    tests.write_bytes(b"# Tests \xff\n")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(tests))}: isn't UTF-8 text\\.$"):
        read_skill_library(str(tmp_path), tmp_path)


def test_a_file_is_read_as_written(tmp_path: Path) -> None:
    script = write(skill_folder(tmp_path, "python") / "run.bat", "")
    script.write_bytes("echo café\r\n".encode())
    assert read(tmp_path)["python"]["run.bat"].text == "echo café\r\n"


def test_a_folder_without_a_skill_md_is_refused_naming_the_folder(tmp_path: Path) -> None:
    write(tmp_path / "python" / "tests.md", "# Tests\n")
    folder = re.escape(str(tmp_path / "python"))
    with pytest.raises(ConfigError, match=f"^{folder}: a skill needs a SKILL\\.md\\.$"):
        read_skill_library(str(tmp_path), tmp_path)


def test_an_invalid_skill_md_is_refused_naming_it(tmp_path: Path) -> None:
    path = write(tmp_path / "python" / "SKILL.md", skill_md("pyton"))
    problem = re.escape(f"{path}: its name is pyton but its folder is python.")
    with pytest.raises(ConfigError, match=f"^{problem}$"):
        read_skill_library(str(tmp_path), tmp_path)
