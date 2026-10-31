"""The skills in effect in a workspace: those shipped with orbit, read from the folders in the
package's skills/, with the project's own in .orbit/skills/ over them."""

import errno
import os
import re
from pathlib import Path

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE, Layer
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


def project_skills(root: Path) -> Path:
    """The folder of the project's own skills in the workspace at ``root``."""
    return root / ".orbit" / "skills"


def project_file(path: Path, text: str) -> SkillFile:
    """The project's skill file at ``path``, holding ``text``."""
    return SkillFile(text=text, layer=Layer("project", str(path)), location=str(path))


def shipped(name: str) -> dict[str, str]:
    """The text of each file of the skill ``name`` orbit ships, by its path in the folder."""
    folder = SKILL_DIRECTORY / name
    return {entry.name: entry.read_text(encoding="utf-8") for entry in folder.iterdir()}


def read(directory: Path) -> dict[str, dict[str, SkillFile]]:
    """The files of each skill read from ``directory``, by skill and path."""
    library = read_skill_library(str(directory), directory)
    return {name: dict(skill.files) for name, skill in library.skills.items()}


def test_the_package_holds_a_folder_for_each_skill(tmp_path: Path) -> None:
    folders = [entry for entry in SKILL_DIRECTORY.iterdir() if entry.is_dir()]
    assert {folder.name for folder in folders} == set(read_skill_library(str(tmp_path)).skills)


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


# The project's skills


def test_without_the_projects_skills_the_skills_are_the_packages(tmp_path: Path) -> None:
    (tmp_path / ".orbit").mkdir()
    skills = read_skill_library(str(tmp_path)).skills
    assert len(skills) == 18
    assert {file.layer for skill in skills.values() for file in skill.files.values()} == {PACKAGE}


def test_a_project_file_replaces_the_shipped_file_at_its_path(tmp_path: Path) -> None:
    tests = write(project_skills(tmp_path) / "python" / "tests.md", "# Our tests\n")
    others = dict(read_skill_library(str(tmp_path)).skills["python"].files)
    assert others.pop("tests.md") == project_file(tests, "# Our tests\n")
    texts = shipped("python")
    del texts["tests.md"]
    assert {path: file.text for path, file in others.items()} == texts
    assert {file.layer for file in others.values()} == {PACKAGE}


def test_a_project_file_at_a_new_path_is_added_to_the_shipped_skill(tmp_path: Path) -> None:
    extra = write(project_skills(tmp_path) / "python" / "extra.md", "# Extra\n")
    files = read_skill_library(str(tmp_path)).skills["python"].files
    assert files["extra.md"] == project_file(extra, "# Extra\n")
    assert set(files) == {*shipped("python"), "extra.md"}


def test_a_project_file_in_a_subfolder_is_added_under_its_relative_path(tmp_path: Path) -> None:
    script = write(project_skills(tmp_path) / "python" / "scripts" / "run.sh", "#!/bin/sh\n")
    files = read_skill_library(str(tmp_path)).skills["python"].files
    assert files["scripts/run.sh"] == project_file(script, "#!/bin/sh\n")


def test_a_project_skill_md_gives_the_shipped_skill_its_description_and_body(
    tmp_path: Path,
) -> None:
    text = "---\nname: python\ndescription: Our Python.\n---\n\n# Our Python\n"
    write(project_skills(tmp_path) / "python" / "SKILL.md", text)
    python = read_skill_library(str(tmp_path)).skills["python"]
    assert (python.description, python.body) == ("Our Python.", "# Our Python\n")


def test_a_project_skill_md_naming_another_skill_is_refused_naming_it(tmp_path: Path) -> None:
    path = write(project_skills(tmp_path) / "python" / "SKILL.md", skill_md("pyton"))
    problem = re.escape(f"{path}: its name is pyton but its folder is python.")
    with pytest.raises(ConfigError, match=f"^{problem}$"):
        read_skill_library(str(tmp_path))


def test_a_project_folder_of_a_new_name_is_a_skill(tmp_path: Path) -> None:
    folder = skill_folder(project_skills(tmp_path), "house-rules")
    house_rules = read_skill_library(str(tmp_path)).skills["house-rules"]
    assert house_rules.description == "The house-rules skill."
    assert house_rules.files == {
        "SKILL.md": project_file(folder / "SKILL.md", skill_md("house-rules"))
    }


def test_a_project_folder_of_a_new_name_without_a_skill_md_is_refused(tmp_path: Path) -> None:
    notes = write(project_skills(tmp_path) / "house-rules" / "notes.md", "# Notes\n")
    folder = re.escape(str(notes.parent))
    with pytest.raises(ConfigError, match=f"^{folder}: a skill needs a SKILL\\.md\\.$"):
        read_skill_library(str(tmp_path))


def test_files_beside_the_project_folders_and_hidden_ones_are_ignored(tmp_path: Path) -> None:
    write(project_skills(tmp_path) / "README.md", "Our skills.\n")
    write(project_skills(tmp_path) / "python" / ".swp", "Swap.\n")
    write(project_skills(tmp_path) / ".drafts" / "notes.md", "Drafts.\n")
    skills = read_skill_library(str(tmp_path)).skills
    assert len(skills) == 18
    assert set(skills["python"].files) == set(shipped("python"))


def test_a_project_file_that_isnt_utf8_is_refused_naming_it(tmp_path: Path) -> None:
    tests = write(project_skills(tmp_path) / "python" / "tests.md", "")
    tests.write_bytes(b"# Tests \xff\n")
    with pytest.raises(ConfigError, match=f"^{re.escape(str(tests))}: isn't UTF-8 text\\.$"):
        read_skill_library(str(tmp_path))


def test_a_dangling_symlink_in_a_project_folder_is_refused_naming_it(tmp_path: Path) -> None:
    tests = project_skills(tmp_path) / "python" / "tests.md"
    tests.parent.mkdir(parents=True)
    tests.symlink_to(tmp_path / "missing.md")
    problem = re.escape(f"{tests}: couldn't be read: {os.strerror(errno.ENOENT)}.")
    with pytest.raises(ConfigError, match=f"^{problem}$"):
        read_skill_library(str(tmp_path))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_project_file_that_cant_be_read_is_refused_naming_it(tmp_path: Path) -> None:
    tests = write(project_skills(tmp_path) / "python" / "tests.md", "# Tests\n")
    tests.chmod(0o000)
    problem = re.escape(f"{tests}: couldn't be read: {os.strerror(errno.EACCES)}.")
    try:
        with pytest.raises(ConfigError, match=f"^{problem}$"):
            read_skill_library(str(tmp_path))
    finally:
        tests.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists any folder")
def test_a_project_folder_that_cant_be_listed_is_refused_naming_it(tmp_path: Path) -> None:
    folder = write(project_skills(tmp_path) / "python" / "tests.md", "# Tests\n").parent
    folder.chmod(0o000)
    problem = re.escape(f"{folder}: couldn't be listed: {os.strerror(errno.EACCES)}.")
    try:
        with pytest.raises(ConfigError, match=f"^{problem}$"):
            read_skill_library(str(tmp_path))
    finally:
        folder.chmod(0o755)
