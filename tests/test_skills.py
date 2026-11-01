"""Skills: folders of files, each opening with a SKILL.md whose front matter names and describes
it, checked as Claude Code checks them."""

import re

import pytest
from pydantic import ValidationError

from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.layers import PACKAGE, Layer
from orbit.agent.domain.skills import (
    NO_SKILLS,
    FrontMatter,
    Skill,
    SkillFile,
    SkillFolder,
    SkillLibrary,
    SkillMapping,
    check_role_skills,
    check_skill_mappings,
    given_skill_file,
    given_skills,
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


def test_a_byte_order_mark_before_the_front_matter_is_left_out() -> None:
    text = "\ufeff---\nname: a\ndescription: b\n---\nBody"
    assert parse_front_matter(text) == FrontMatter(name="a", description="b", body="Body")


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ('"python"', "python"),
        ("'python'", "python"),
        ('"Use when: writing Python"', "Use when: writing Python"),
        ("'Use when: writing Python'", "Use when: writing Python"),
        ("  ' Spaced '  ", " Spaced "),
        ("\"'Single'\"", "'Single'"),
        ("'\"Double\"'", '"Double"'),
        ('">"', ">"),
    ],
)
def test_a_value_in_matching_quotes_loses_them(written: str, value: str) -> None:
    front = parse_front_matter(f"---\nname: {written}\ndescription: {written}\n---\n")
    assert (front.name, front.description) == (value, value)


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ("'It''s Python'", "It's Python"),
        ("''''", "'"),
        ("'C:\\temp'", "C:\\temp"),
        ('"Say \\"hi\\""', 'Say "hi"'),
        ('"C:\\\\temp"', "C:\\temp"),
        ('"\\\\\\""', '\\"'),
        ("\"It''s\"", "It''s"),
    ],
)
def test_single_quotes_double_a_quote_and_double_quotes_escape_quotes_and_backslashes(
    written: str, value: str
) -> None:
    assert parse_front_matter(f"---\nname: a\ndescription: {written}\n---\n").description == value


@pytest.mark.parametrize(
    "written", ['"Python', "Python'", "'Python", 'Python"', "\"Python'", "'Python\"", '"', "'"]
)
def test_a_value_with_one_quote_or_mismatched_quotes_is_kept_as_written(written: str) -> None:
    assert parse_front_matter(f"---\nname: a\ndescription: {written}\n---\n").description == (
        written
    )


@pytest.mark.parametrize(
    ("indicator", "description"),
    [
        (">", "Python conventions. Use when: writing Python."),
        (">-", "Python conventions. Use when: writing Python."),
        (">+", "Python conventions. Use when: writing Python."),
        ("|", "Python conventions.\nUse when: writing Python."),
        ("|-", "Python conventions.\nUse when: writing Python."),
        ("|+", "Python conventions.\nUse when: writing Python."),
    ],
)
def test_a_block_scalar_is_its_indented_lines_folded_by_a_chevron_or_kept_by_a_bar(
    indicator: str, description: str
) -> None:
    text = (
        f"---\ndescription: {indicator}\n  Python conventions.\n  Use when: writing Python.\n"
        "name: a\n---\nBody"
    )
    assert parse_front_matter(text) == FrontMatter("a", description, "Body")


def test_a_block_scalars_lines_may_end_with_carriage_returns() -> None:
    text = "---\r\nname: a\r\ndescription: |\r\n  b\r\n  c\r\n---\r\nBody"
    assert parse_front_matter(text).description == "b\nc"


def test_a_block_scalars_lines_that_look_like_keys_are_its_text() -> None:
    text = "---\nname: a\ndescription: >\n  name: b\n---\nBody"
    assert parse_front_matter(text) == FrontMatter("a", "name: b", "Body")


def test_a_block_scalar_without_indented_lines_is_empty() -> None:
    assert parse_front_matter("---\nname: a\ndescription: >\n---\nBody").description == ""


@pytest.mark.parametrize("written", ["> Python", "| Python", ">>", "|>"])
def test_a_value_that_only_starts_like_a_block_scalar_is_kept_as_written(written: str) -> None:
    text = f"---\nname: a\ndescription: {written}\n  Indented.\n---\nBody"
    assert parse_front_matter(text).description == written


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


def test_a_block_scalar_description_without_indented_lines_is_refused() -> None:
    problem = "a skill's description is 1 to 1,024 characters."
    with pytest.raises(ConfigError, match=refused(problem)):
        load(skill_md(description=">"))


@pytest.mark.parametrize("name", ['"python"', "'python'"])
def test_a_quoted_name_matching_the_folder_is_taken(name: str) -> None:
    skill = load(skill_md(name=name, description='"Use when: writing Python"'))
    assert (skill.name, skill.description) == ("python", "Use when: writing Python")


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


def check(
    *mappings: SkillMapping, present: tuple[str, ...] = ("src",), escaping: tuple[str, ...] = ()
) -> None:
    """Check ``mappings`` against the python skill's library, ``present`` being the paths in the
    project and ``escaping`` those whose links lead out of it."""
    check_skill_mappings(mappings, library("python"), present, escaping)


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


@pytest.mark.parametrize("path", ["/etc", "../other", "..", "src/../../other", ""])
def test_a_path_that_isnt_relative_or_leaves_the_project_is_refused(path: str) -> None:
    with pytest.raises(ConfigError, match=whole(OUTSIDE)):
        check(mapping(path), present=(path,))


@pytest.mark.parametrize("path", ["src\x00", "src/\x00/lib", "\x00"])
def test_a_path_holding_a_nul_byte_is_refused(path: str) -> None:
    with pytest.raises(ConfigError, match=whole(OUTSIDE)):
        check(mapping(path), present=(path,))


def test_an_empty_path_is_refused_naming_its_entry() -> None:
    problem = "skills.2.paths: paths are relative to the project root and stay inside it."
    with pytest.raises(ConfigError, match=whole(problem)):
        check(mapping("src"), mapping(""), present=("src", ""))


def test_the_project_root_named_as_a_dot_passes() -> None:
    check(mapping("."), present=(".",))


def test_a_path_through_dot_dot_that_stays_inside_the_project_passes() -> None:
    check(mapping("src/../src"), present=("src/../src",))


def test_a_path_whose_links_lead_out_of_the_project_is_refused_naming_its_entry() -> None:
    problem = "skills.2.paths: paths are relative to the project root and stay inside it."
    with pytest.raises(ConfigError, match=whole(problem)):
        check(mapping("src"), mapping("out"), present=("src", "out"), escaping=("out",))


@pytest.mark.parametrize(
    ("paths", "first"),
    [
        (("lib", "out"), "skills.1.paths names lib, which isn't in the project."),
        (("out", "lib"), OUTSIDE),
    ],
)
def test_a_path_leading_out_and_a_missing_one_are_refused_in_order(
    paths: tuple[str, ...], first: str
) -> None:
    with pytest.raises(ConfigError, match=whole(first)):
        check(mapping(*paths), present=("out",), escaping=("out",))


def test_a_path_not_in_the_project_is_refused_naming_its_entry_and_the_path() -> None:
    problem = "skills.2.paths names src/orbt, which isn't in the project."
    with pytest.raises(ConfigError, match=whole(problem)):
        check(mapping("src"), mapping("src", "src/orbt"))


def test_a_skill_not_in_the_library_is_refused_listing_the_skills_in_name_order() -> None:
    problem = "skills.1.use names pyton, which isn't a skill; the skills are cpp, go, python."
    with pytest.raises(ConfigError, match=whole(problem)):
        check_skill_mappings(
            [mapping("src", use=("pyton",))], library("python", "go", "cpp"), ["src"], []
        )


def test_an_entrys_paths_are_checked_in_order_before_its_skills() -> None:
    first = "skills.1.paths names lib, which isn't in the project."
    with pytest.raises(ConfigError, match=whole(first)):
        check(mapping("src", "lib", "/etc", use=("pyton",)))


def test_an_entry_is_checked_whole_before_the_next() -> None:
    first = "skills.1.use names pyton, which isn't a skill; the skills are python."
    with pytest.raises(ConfigError, match=whole(first)):
        check(mapping("src", use=("python", "pyton")), mapping("/etc"))


# Skills by role


def test_a_roles_skills_in_the_library_pass() -> None:
    check_role_skills("coder", ("python", "cpp"), library("python", "cpp"))


def test_a_role_naming_no_skills_passes() -> None:
    check_role_skills("coder", (), library())


def test_a_roles_first_unknown_skill_is_refused_naming_the_role_and_listing_the_skills() -> None:
    problem = "roles.coder.skills names pyton, which isn't a skill; the skills are cpp, go, python."
    with pytest.raises(ConfigError, match=whole(problem)):
        check_role_skills("coder", ("python", "pyton", "rust"), library("python", "go", "cpp"))


# Skills given to an agent


def test_an_agent_is_given_its_roles_skills_then_the_mapped_ones_in_order() -> None:
    mappings = (mapping("src", use=("python", "application")), mapping("native", use=("cpp",)))
    assert given_skills(("code-style", "security"), mappings) == (
        "code-style",
        "security",
        "python",
        "application",
        "cpp",
    )


def test_a_skill_from_the_role_and_a_mapping_is_given_once_where_first_named() -> None:
    mappings = (mapping("src", use=("python", "security")), mapping("tests", use=("python",)))
    assert given_skills(("security",), mappings) == ("security", "python")


def test_a_role_without_skills_and_no_mappings_gives_none() -> None:
    assert given_skills((), ()) == ()


# Reading a skill's files


def skill_of(name: str, *paths: str) -> Skill:
    """The skill ``name``, with its SKILL.md and a file at each of ``paths``, in that order,
    each holding its skill's name and its path."""
    files = {
        path: SkillFile(text=f"{name} {path}\n", layer=PACKAGE, location=f"/s/{name}/{path}")
        for path in ("SKILL.md", *paths)
    }
    return Skill(name=name, description=f"{name}.", body="", files=files)


FILES = SkillLibrary(
    skills={
        "code-style": skill_of("code-style", "testing.md", "naming.md"),
        "python": skill_of("python", "tests.md"),
        "security": skill_of("security"),
    }
)
"""A library of code-style, python and security, code-style's files not in order."""


def test_a_file_of_a_given_skill_is_readable() -> None:
    found = given_skill_file(("code-style",), FILES, "code-style", "testing.md")
    assert found == FILES.skills["code-style"].files["testing.md"]


def test_a_given_skills_skill_md_is_readable() -> None:
    found = given_skill_file(("python",), FILES, "python", "SKILL.md")
    assert isinstance(found, SkillFile) and found.text == "python SKILL.md\n"


def test_an_agent_given_no_skills_is_told_so() -> None:
    assert given_skill_file((), FILES, "python", "tests.md") == NO_SKILLS
    assert NO_SKILLS == "You weren't given any skills."


def test_a_skill_not_given_is_refused_naming_it_and_listing_the_given_in_order() -> None:
    assert given_skill_file(("security", "code-style"), FILES, "python", "tests.md") == (
        "You weren't given the skill python; your skills are security, code-style."
    )


def test_a_given_skill_the_library_lacks_is_neither_read_nor_listed() -> None:
    given = ("code-style", "added-since")
    assert given_skill_file(given, FILES, "added-since", "SKILL.md") == (
        "You weren't given the skill added-since; your skills are code-style."
    )


def test_given_skills_the_library_wholly_lacks_count_as_none() -> None:
    assert given_skill_file(("added-since",), FILES, "added-since", "SKILL.md") == NO_SKILLS


@pytest.mark.parametrize("path", ["nope.md", "../python/tests.md", "/etc/passwd", "./testing.md"])
def test_a_file_the_skill_lacks_is_refused_listing_its_files_sorted(path: str) -> None:
    assert given_skill_file(("code-style",), FILES, "code-style", path) == (
        f"code-style has no file {path}; its files are SKILL.md, naming.md, testing.md."
    )
