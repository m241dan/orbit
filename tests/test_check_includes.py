"""``cpp``'s include checker, ``scripts/check_includes.py``: each C and C++ file's includes against
the prefixes ``include-rules.toml`` allows its directory, run on temporary trees."""

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from orbit.agent.external.skill_files import SKILL_DIRECTORY, read_skill_library

SCRIPT = Path(str(SKILL_DIRECTORY / "cpp" / "scripts" / "check_includes.py"))
"""The include checker, as the ``cpp`` skill ships it."""

RULES = '[rules]\n"src/domain" = ["proj/domain"]\n"src" = ["proj"]\n'
"""Rules letting ``src/domain`` include ``proj/domain`` and the rest of ``src`` include ``proj``."""

type Main = Callable[[Sequence[str]], int]
"""The checker's ``main``: the exit status for its command line."""


def load_main() -> Main:
    """The include checker's ``main``, run from its file's source rather than imported, so no
    bytecode cache lands in the skill's folder, which serves every file in it."""
    namespace: dict[str, object] = {"__name__": "check_includes"}
    exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), namespace)
    main = namespace["main"]
    assert callable(main)
    return cast(Main, main)


MAIN = load_main()
"""The include checker's ``main``."""


def make_tree(root: Path, files: Mapping[str, str], rules: str | None = RULES) -> Path:
    """``root``, holding each of ``files`` by its path with its text, and ``rules`` as its
    include-rules.toml unless it's None."""
    texts = {**files, "include-rules.toml": rules} if rules is not None else files
    for path, text in texts.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    return root


def check(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    """The checker's exit status on ``root`` and the lines it prints, standard error's after
    standard output's."""
    status = MAIN(["check_includes.py", str(root)])
    printed = capsys.readouterr()
    return status, (printed.out + printed.err).splitlines()


def test_an_include_a_directorys_prefix_allows_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path, {"src/domain/a.cpp": "#include <proj/domain/b.hpp>\n"})
    assert check(tmp_path, capsys) == (0, [])


def test_an_include_no_prefix_allows_fails_with_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path, {"src/domain/a.cpp": "#include <proj/domain/b.hpp>\n#include <asio.hpp>\n"})
    assert check(tmp_path, capsys) == (
        1,
        ["src/domain/a.cpp:2: <asio.hpp> isn't allowed in src/domain"],
    )


def test_a_directory_takes_its_nearest_listed_parents_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path, {"src/x/a.cpp": '#include "proj/app/b.hpp"\n#include <asio.hpp>\n'})
    assert check(tmp_path, capsys) == (1, ["src/x/a.cpp:2: <asio.hpp> isn't allowed in src"])


def test_a_prefix_matches_whole_path_segments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path, {"src/domain/a.cpp": '#  include "proj/domainx/c.hpp"\n'})
    assert check(tmp_path, capsys) == (
        1,
        ['src/domain/a.cpp:1: "proj/domainx/c.hpp" isn\'t allowed in src/domain'],
    )


def test_a_prefix_allows_the_include_it_names_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = '[rules]\n"src" = ["proj/version.hpp", "proj/domain/"]\n'
    files = {"src/a.cpp": "#include <proj/version.hpp>\n#include <proj/domain/b.hpp>\n"}
    make_tree(tmp_path, files, rules)
    assert check(tmp_path, capsys) == (0, [])


@pytest.mark.parametrize("include", ["<vector>", "<stdio.h>", '"stdio.h"', "<string.h>"])
def test_a_standard_header_is_allowed_when_the_rules_list_no_std(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], include: str
) -> None:
    make_tree(tmp_path, {"src/domain/a.cpp": f"#include {include}\n"})
    assert check(tmp_path, capsys) == (0, [])


@pytest.mark.parametrize("include", ['"vector"', "<sys/types.h>", "<asio.hpp>", '"config.h"'])
def test_a_header_isnt_standard_quoted_with_a_slash_or_with_an_extension_c_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], include: str
) -> None:
    make_tree(tmp_path, {"src/domain/a.cpp": f"#include {include}\n"})
    assert check(tmp_path, capsys) == (
        1,
        [f"src/domain/a.cpp:1: {include} isn't allowed in src/domain"],
    )


def test_listing_std_allows_only_the_listed_standard_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = '[rules]\n"src/domain" = ["std", "vector", "proj/domain"]\n'
    files = {"src/domain/a.cpp": "#include <vector>\n#include <map>\n#include <stdio.h>\n"}
    make_tree(tmp_path, files, rules)
    assert check(tmp_path, capsys) == (
        1,
        [
            "src/domain/a.cpp:2: <map> isn't allowed in src/domain",
            "src/domain/a.cpp:3: <stdio.h> isn't allowed in src/domain",
        ],
    )


def test_a_file_under_no_listed_directory_fails_saying_no_rules_cover_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path, {"tools/gen.cpp": "#include <vector>\n", "src/a.cpp": ""})
    assert check(tmp_path, capsys) == (1, ["tools/gen.cpp: no rules cover tools"])


def test_a_file_at_the_root_takes_the_roots_rules_when_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = '[rules]\n"." = ["proj"]\n'
    make_tree(tmp_path, {"main.cpp": "#include <proj/a.hpp>\n#include <asio.hpp>\n"}, rules)
    assert check(tmp_path, capsys) == (1, ["main.cpp:2: <asio.hpp> isn't allowed in ."])


def test_every_violation_is_listed_by_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "src/domain/rules.hpp": "#pragma once\n# include <asio.hpp>\n",
        "src/domain/a.cc": '#include "boost/x.hpp"\nint x;\n#include <fmt/core.h>\n',
        "src/app/b.cxx": "#include <asio.hpp>\n",
        "tools/c.c": "",
    }
    make_tree(tmp_path, files)
    assert check(tmp_path, capsys) == (
        1,
        [
            "src/app/b.cxx:1: <asio.hpp> isn't allowed in src",
            'src/domain/a.cc:1: "boost/x.hpp" isn\'t allowed in src/domain',
            "src/domain/a.cc:3: <fmt/core.h> isn't allowed in src/domain",
            "src/domain/rules.hpp:2: <asio.hpp> isn't allowed in src/domain",
            "tools/c.c: no rules cover tools",
        ],
    )


@pytest.mark.parametrize("suffix", [".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"])
def test_each_c_and_cpp_source_and_header_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], suffix: str
) -> None:
    make_tree(tmp_path, {f"src/a{suffix}": "#include <asio.hpp>\n"})
    assert check(tmp_path, capsys) == (1, [f"src/a{suffix}:1: <asio.hpp> isn't allowed in src"])


def test_other_files_hidden_directories_and_build_trees_are_left_unchecked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "README.md": "#include <asio.hpp>\n",
        "src/gen.py": "#include <asio.hpp>\n",
        ".cache/a.cpp": "#include <asio.hpp>\n",
        "src/.hidden/a.cpp": "#include <asio.hpp>\n",
        "build/asan/CMakeCache.txt": "",
        "build/asan/vcpkg_installed/asio.hpp": "#include <asio/impl.hpp>\n",
    }
    make_tree(tmp_path, files)
    assert check(tmp_path, capsys) == (0, [])


def test_a_symlinked_directory_is_left_unchecked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_tree(tmp_path, {"src/a.cpp": "#include <proj/a.hpp>\n"})
    (tmp_path / "src" / "loop").symlink_to(tmp_path / "src", target_is_directory=True)
    assert check(tmp_path, capsys) == (0, [])


def test_the_root_defaults_to_the_current_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_tree(tmp_path, {"src/a.cpp": "#include <asio.hpp>\n"})
    monkeypatch.chdir(tmp_path)
    assert MAIN(["check_includes.py"]) == 1
    assert capsys.readouterr().out == "src/a.cpp:1: <asio.hpp> isn't allowed in src\n"


def test_missing_rules_exit_2_saying_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_tree(tmp_path, {"src/a.cpp": "#include <asio.hpp>\n"}, rules=None)
    assert check(tmp_path, capsys) == (2, [f"{tmp_path / 'include-rules.toml'}: not found."])


def test_unreadable_rules_exit_2_saying_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "include-rules.toml").mkdir()
    status, printed = check(tmp_path, capsys)
    assert status == 2
    assert printed[0].startswith(f"{tmp_path / 'include-rules.toml'}: couldn't be read: ")


@pytest.mark.parametrize(
    ("rules", "why"),
    [
        ("[rules\n", "isn't valid TOML: "),
        ("", "needs a [rules] table mapping each directory to its"),
        ('rules = "src"\n', "needs a [rules] table mapping each directory to its"),
        ('[rules]\n"src" = ["proj"]\n[skip]\n', "has unknown keys: skip."),
        ('[rules]\n"src" = "proj"\n', 'rules for "src" must be a list of include prefixes.'),
        ('[rules]\n"src" = ["proj", 1]\n', 'rules for "src" must be a list of include prefixes.'),
        ('[rules]\n"../src" = ["proj"]\n', '"../src" isn\'t a directory under the root.'),
        ('[rules]\n"/src" = ["proj"]\n', '"/src" isn\'t a directory under the root.'),
    ],
)
def test_invalid_rules_exit_2_saying_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rules: str, why: str
) -> None:
    make_tree(tmp_path, {"src/a.cpp": ""}, rules)
    status, printed = check(tmp_path, capsys)
    assert status == 2
    assert printed[0].startswith(f"{tmp_path / 'include-rules.toml'}: {why}")


def test_the_checker_runs_on_its_own_with_python(tmp_path: Path) -> None:
    make_tree(tmp_path, {"src/a.cpp": "#include <asio.hpp>\n"})
    ran = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (ran.returncode, ran.stdout) == (1, "src/a.cpp:1: <asio.hpp> isn't allowed in src\n")


def test_the_cpp_skill_serves_the_checker_as_a_file(tmp_path: Path) -> None:
    files = read_skill_library(str(tmp_path)).skills["cpp"].files
    assert files["scripts/check_includes.py"].text == SCRIPT.read_text(encoding="utf-8")
