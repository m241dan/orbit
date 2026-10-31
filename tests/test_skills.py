"""Skills: folders of files, each opening with a SKILL.md whose front matter names and describes
it, checked as Claude Code checks them."""

import re

import pytest

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE
from orbit.agent.domain.skills import (
    FrontMatter,
    Skill,
    SkillFile,
    SkillLibrary,
    load_skill,
    parse_front_matter,
)

FOLDER = "/orbit/skills/python"
SKILL_MD = f"{FOLDER}/SKILL.md"
NEEDS_FRONT_MATTER = "needs front matter between --- lines, with name and description."


def skill_md(name: str = "python", description: str = "Python conventions.") -> str:
    """A SKILL.md naming and describing its skill, with a body."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Python\n"


def skill_file(text: str, path: str = "SKILL.md") -> SkillFile:
    """The package's file at ``path`` in the python skill's folder, holding ``text``."""
    return SkillFile(text=text, layer=PACKAGE, location=f"{FOLDER}/{path}")


def load(text: str, folder: str = "python") -> Skill:
    """The skill in ``folder`` whose SKILL.md holds ``text``."""
    return load_skill(folder, FOLDER, {"SKILL.md": skill_file(text)})


def refused(problem: str, location: str = SKILL_MD) -> str:
    """The pattern of the error naming ``location`` for ``problem``, matched whole."""
    return f"^{re.escape(f'{location}: {problem}')}$"


# Front matter


def test_front_matter_gives_the_name_description_and_body_and_ignores_other_keys() -> None:
    text = "---\nname: a\ndescription: b\nallowed-tools: x\n---\nBody"
    assert parse_front_matter(text) == FrontMatter(name="a", description="b", body="Body")


def test_the_body_starts_after_the_blank_lines_below_the_front_matter() -> None:
    assert parse_front_matter("---\nname: a\ndescription: b\n---\n\n\n# A\n\nText.\n").body == (
        "# A\n\nText.\n"
    )


def test_a_value_keeps_its_colons_and_loses_the_spaces_around_it() -> None:
    front = parse_front_matter("---\nname:  a \ndescription: Use: always\n---\n")
    assert (front.name, front.description, front.body) == ("a", "Use: always", "")


def test_lines_that_arent_keys_and_indented_keys_are_ignored() -> None:
    text = "---\nname: a\nmetadata:\n  name: b\n  - c\n\ndescription: d\n---\nBody"
    assert parse_front_matter(text) == FrontMatter(name="a", description="d", body="Body")


def test_front_matter_may_end_its_lines_with_carriage_returns() -> None:
    text = "---\r\nname: a\r\ndescription: b\r\n---\r\n\r\nBody\r\n"
    assert parse_front_matter(text) == FrontMatter(name="a", description="b", body="Body\r\n")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "name: a\ndescription: b\n---\nBody",
        "# A\n---\nname: a\ndescription: b\n---\nBody",
        "---\nname: a\ndescription: b\nBody",
        "---\ndescription: b\n---\nBody",
        "---\nname: a\n---\nBody",
        "---\nname a\ndescription b\n---\nBody",
    ],
)
def test_front_matter_without_both_lines_or_without_a_name_or_description_is_refused(
    text: str,
) -> None:
    with pytest.raises(ConfigError, match=f"^{re.escape(NEEDS_FRONT_MATTER)}$"):
        parse_front_matter(text)


# Skills


def test_a_skill_has_its_front_matters_name_and_description_and_every_file() -> None:
    files = {"tests.md": skill_file("Tests.\n", "tests.md"), "SKILL.md": skill_file(skill_md())}
    skill = load_skill("python", FOLDER, files)
    assert (skill.name, skill.description) == ("python", "Python conventions.")
    assert skill.body == "# Python\n"
    assert skill.files == files


def test_a_skills_files_are_in_order_of_their_paths() -> None:
    files = {
        path: skill_file(skill_md() if path == "SKILL.md" else "Text.\n", path)
        for path in ("tests.md", "scripts/run.sh", "SKILL.md", "async.md")
    }
    paths = list(load_skill("python", FOLDER, files).files)
    assert paths == ["SKILL.md", "async.md", "scripts/run.sh", "tests.md"]


def test_a_folder_without_a_skill_md_is_refused_naming_the_folder() -> None:
    files = {"tests.md": skill_file("Tests.\n", "tests.md")}
    with pytest.raises(ConfigError, match=refused("a skill needs a SKILL.md.", FOLDER)):
        load_skill("python", FOLDER, files)


def test_a_skill_md_without_front_matter_is_refused_naming_it() -> None:
    with pytest.raises(ConfigError, match=refused(NEEDS_FRONT_MATTER)):
        load("# Python\n")


def test_a_name_other_than_the_folders_is_refused() -> None:
    with pytest.raises(ConfigError, match=refused("its name is pyton but its folder is python.")):
        load(skill_md(name="pyton"))


@pytest.mark.parametrize("name", ["Bad_Name", "a" * 65, "naïve", "two words"])
def test_a_name_claude_code_doesnt_take_is_refused(name: str) -> None:
    problem = "a skill's name is at most 64 lowercase letters, digits and hyphens."
    with pytest.raises(ConfigError, match=refused(problem)):
        load(skill_md(name=name), folder=name)


@pytest.mark.parametrize("description", ["", "d" * 1025])
def test_a_description_claude_code_doesnt_take_is_refused(description: str) -> None:
    problem = "a skill's description is 1 to 1,024 characters."
    with pytest.raises(ConfigError, match=refused(problem)):
        load(skill_md(description=description))


def test_a_name_of_64_characters_and_a_description_of_1024_are_taken() -> None:
    name = "a1-" * 21 + "z"
    skill = load(skill_md(name=name, description="d" * 1024), folder=name)
    assert (len(skill.name), len(skill.description)) == (64, 1024)


# The library


def test_the_library_is_empty_by_default() -> None:
    assert SkillLibrary().skills == {}


def test_the_librarys_skills_are_in_name_order() -> None:
    skills = {name: load(skill_md(name=name), folder=name) for name in ("python", "cpp", "go")}
    assert list(SkillLibrary(skills=skills).skills) == ["cpp", "go", "python"]
