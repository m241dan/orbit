"""The project's mapped code: the paths its ``[[skills]]`` map, and which files are code under
them."""

from collections.abc import Sequence

import pytest

from orbit.agent.domain.mapped_paths import (
    CODE_EXTENSIONS,
    is_mapped,
    is_mapped_code,
    list_mapped_paths,
)
from orbit.agent.domain.skills import SkillMapping


@pytest.mark.parametrize(
    ("path", "mapped", "code"),
    [
        ("src/a.py", ["."], True),
        ("src/a.py", [], True),
        ("src/pkg/a.py", ["src"], True),
        ("srcs/a.py", ["src"], False),
        ("tests/a.py", ["src"], False),
        ("tools/run.py", ["tools/run.py"], True),
        ("tools/other.py", ["tools/run.py"], False),
        ("src/a.py", ["src/"], True),
        ("src/a.py", ["./src"], True),
        ("src/a.py", ["tests/../src"], True),
        ("src/a.py", ["tests", "src/pkg/.."], True),
        ("src/a.md", ["src"], False),
        ("src/a.md", ["."], False),
        ("src/a.md", [], False),
    ],
)
def test_a_path_is_mapped_code_when_it_has_a_code_extension_under_a_mapped_path(
    path: str, mapped: Sequence[str], code: bool
) -> None:
    assert is_mapped_code(path, mapped) is code


@pytest.mark.parametrize(
    ("path", "mapped", "held"),
    [
        ("docs/a.md", ["."], True),
        ("docs/a.md", [], True),
        ("src/a.md", ["src/"], True),
        ("src", ["src"], True),
        ("docs/a.md", ["src"], False),
    ],
)
def test_a_path_is_mapped_whatever_its_extension_when_under_a_mapped_path(
    path: str, mapped: Sequence[str], held: bool
) -> None:
    assert is_mapped(path, mapped) is held


def test_the_code_extensions_are_the_built_in_source_ones() -> None:
    assert set(CODE_EXTENSIONS) == {
        ".py",
        ".pyi",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".cs",
        ".swift",
        ".rb",
    }


def test_the_mapped_paths_are_those_of_every_mapping_in_order() -> None:
    mappings = (
        SkillMapping(paths=("src", "tests"), use=("python",)),
        SkillMapping(paths=("web",), use=("typescript",)),
        SkillMapping(paths=("src/ui", "docs"), use=("ui",)),
    )
    assert list_mapped_paths(mappings) == ("src", "tests", "web", "src/ui", "docs")


def test_no_mappings_map_no_paths() -> None:
    assert list_mapped_paths(()) == ()
