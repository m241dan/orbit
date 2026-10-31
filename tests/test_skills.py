"""Skills: folders of files, each opening with a SKILL.md whose front matter names and describes
it, checked as Claude Code checks them."""

import re

import pytest
from pydantic import ValidationError

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE, Layer
from orbit.agent.domain.skills import (
    FrontMatter,
    Skill,
    SkillFile,
    SkillFolder,
    SkillLibrary,
    SkillMapping,
    check_skill_mappings,
    load_skill,
    load_skill_library,
    parse_front_matter,
)

FOLDER = "/orbit/skills/python"
PROJECT = "/src/app/.orbit/skills"
SKILL_MD = f"{FOLDER}/SKILL.md"
NEEDS_FRONT_MATTER = "needs front matter between --- lines, with name and description."


def skill_md(name: str = "python", description: str = "Python conventions.") -> str:
    """A SKILL.md naming and describing its skill, with a body."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Python\n"


def skill_file(text: str, path: str = "SKILL.md") -> SkillFile:
    """The package's file at ``path`` in the python skill's folder, holding ``text``."""
    return SkillFile(text=text, layer=PACKAGE, location=f"{FOLDER}/{path}")


def project_file(text: str, path: str, folder: str = "python") -> SkillFile:
    """The project's file at ``path`` in its folder for the skill ``folder``, holding ``text``."""
    location = f"{PROJECT}/{folder}/{path}"
    return SkillFile(text=text, layer=Layer("project", location), location=location)


def shipped(*paths: str) -> dict[str, SkillFolder]:
    """The package's python skill, its SKILL.md and a file at each of ``paths``."""
    files = {path: skill_file(f"Shipped {path}.\n", path) for path in paths}
    return {"python": SkillFolder(FOLDER, {"SKILL.md": skill_file(skill_md()), **files})}


def projects(folder: str, files: dict[str, SkillFile]) -> dict[str, SkillFolder]:
    """The project's folder for the skill ``folder``, holding ``files``."""
    return {folder: SkillFolder(f"{PROJECT}/{folder}", files)}


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


# The layers


def test_without_the_projects_folders_the_library_is_the_packages() -> None:
    library = load_skill_library(shipped("tests.md"), {})
    assert list(library.skills) == ["python"]
    assert library.skills["python"].files == dict(shipped("tests.md")["python"].files)


def test_a_project_file_replaces_the_packages_at_its_path_whole() -> None:
    tests = project_file("Our tests.\n", "tests.md")
    library = load_skill_library(
        shipped("tests.md", "docs.md"), projects("python", {"tests.md": tests})
    )
    files = library.skills["python"].files
    assert files["tests.md"] == tests
    assert files["docs.md"] == skill_file("Shipped docs.md.\n", "docs.md")
    assert list(files) == ["SKILL.md", "docs.md", "tests.md"]


def test_a_project_folder_without_a_skill_md_keeps_the_packages() -> None:
    tests = project_file("Our tests.\n", "tests.md")
    skill = load_skill_library(shipped(), projects("python", {"tests.md": tests})).skills["python"]
    assert skill.files["SKILL.md"] == skill_file(skill_md())
    assert skill.description == "Python conventions."


def test_a_project_file_at_a_new_path_is_added_to_the_packages() -> None:
    extra = project_file("Extra.\n", "extra.md")
    library = load_skill_library(shipped("tests.md"), projects("python", {"extra.md": extra}))
    files = library.skills["python"].files
    assert list(files) == ["SKILL.md", "extra.md", "tests.md"]
    assert files["extra.md"] == extra


def test_a_project_skill_md_gives_the_skill_its_front_matter_and_body() -> None:
    ours = project_file(skill_md(description="Our Python."), "SKILL.md")
    library = load_skill_library(shipped(), projects("python", {"SKILL.md": ours}))
    skill = library.skills["python"]
    assert (skill.description, skill.body) == ("Our Python.", "# Python\n")
    assert skill.files["SKILL.md"] == ours


def test_a_project_skill_md_is_validated_naming_it() -> None:
    ours = project_file(skill_md(name="pyton"), "SKILL.md")
    problem = "its name is pyton but its folder is python."
    with pytest.raises(ConfigError, match=refused(problem, ours.location)):
        load_skill_library(shipped(), projects("python", {"SKILL.md": ours}))


def test_a_project_folder_of_a_new_name_is_a_skill_of_its_own() -> None:
    rules = project_file(skill_md(name="house-rules"), "SKILL.md", folder="house-rules")
    library = load_skill_library(shipped(), projects("house-rules", {"SKILL.md": rules}))
    assert list(library.skills) == ["house-rules", "python"]
    assert library.skills["house-rules"].files == {"SKILL.md": rules}


def test_a_project_folder_of_a_new_name_without_a_skill_md_is_refused_naming_it() -> None:
    notes = project_file("Notes.\n", "notes.md", folder="house-rules")
    problem = refused("a skill needs a SKILL.md.", f"{PROJECT}/house-rules")
    with pytest.raises(ConfigError, match=problem):
        load_skill_library(shipped(), projects("house-rules", {"notes.md": notes}))


# Skills by path

OUTSIDE = "skills.1.paths: paths are relative to the project root and stay inside it."


def library(*names: str) -> SkillLibrary:
    """The library of a skill of each of ``names``."""
    return SkillLibrary(skills={name: load(skill_md(name=name), folder=name) for name in names})


def check(*mappings: SkillMapping, present: tuple[str, ...] = ("src",)) -> None:
    """Check ``mappings`` against the python skill's library, ``present`` being the paths in the
    project."""
    check_skill_mappings(mappings, library("python"), present)


def mapping(*paths: str, use: tuple[str, ...] = ("python",)) -> SkillMapping:
    """The mapping of ``paths`` to the skills ``use`` names."""
    return SkillMapping(paths=paths, use=use)


def whole(message: str) -> str:
    """The pattern matching ``message`` whole."""
    return f"^{re.escape(message)}$"


def test_a_mapping_takes_its_paths_and_skills_as_tuples() -> None:
    taken = SkillMapping.model_validate({"paths": ["src", "noxfile.py"], "use": ["python"]})
    assert taken == SkillMapping(paths=("src", "noxfile.py"), use=("python",))


@pytest.mark.parametrize("data", [{"paths": [], "use": ["python"]}, {"paths": ["src"], "use": []}])
def test_a_mapping_without_paths_or_skills_is_refused(data: dict[str, list[str]]) -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        SkillMapping.model_validate(data)


def test_a_mapping_with_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SkillMapping.model_validate({"paths": ["src"], "use": ["python"], "path": "src"})


def test_mappings_to_paths_in_the_project_and_skills_in_the_library_pass() -> None:
    check(mapping("src"), mapping("src", "noxfile.py"), present=("src", "noxfile.py"))


def test_no_mappings_pass() -> None:
    check()


@pytest.mark.parametrize("path", ["/etc", "../other", "..", "src/../../other"])
def test_a_path_that_isnt_relative_or_leaves_the_project_is_refused(path: str) -> None:
    with pytest.raises(ConfigError, match=whole(OUTSIDE)):
        check(mapping(path), present=(path,))


def test_a_path_through_dot_dot_that_stays_inside_the_project_passes() -> None:
    check(mapping("src/../src"), present=("src/../src",))


def test_a_path_not_in_the_project_is_refused_naming_its_entry_and_the_path() -> None:
    problem = "skills.2.paths names src/orbt, which isn't in the project."
    with pytest.raises(ConfigError, match=whole(problem)):
        check(mapping("src"), mapping("src", "src/orbt"))


def test_a_skill_not_in_the_library_is_refused_listing_the_skills_in_name_order() -> None:
    problem = "skills.1.use names pyton, which isn't a skill; the skills are cpp, go, python."
    with pytest.raises(ConfigError, match=whole(problem)):
        check_skill_mappings(
            [mapping("src", use=("pyton",))], library("python", "go", "cpp"), ["src"]
        )


def test_an_entrys_paths_are_checked_in_order_before_its_skills() -> None:
    first = "skills.1.paths names lib, which isn't in the project."
    with pytest.raises(ConfigError, match=whole(first)):
        check(mapping("src", "lib", "/etc", use=("pyton",)))


def test_an_entry_is_checked_whole_before_the_next() -> None:
    first = "skills.1.use names pyton, which isn't a skill; the skills are python."
    with pytest.raises(ConfigError, match=whole(first)):
        check(mapping("src", use=("python", "pyton")), mapping("/etc"))
