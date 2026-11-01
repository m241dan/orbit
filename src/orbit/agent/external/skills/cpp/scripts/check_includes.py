"""Check that each C and C++ file under a project's root includes only what include-rules.toml
allows its directory, and list every include that isn't allowed.

Run as ``python3 tools/check_includes.py [root]``, the root defaulting to the current directory.
``<root>/include-rules.toml`` maps each directory, relative to the root, to the include prefixes
it allows, and a directory not listed takes its nearest listed parent's::

    [rules]
    "src/domain" = ["myproj/domain"]
    "src" = ["myproj"]

A prefix allows the includes under it by whole path segments. Standard headers are allowed too,
unless a directory's rules list ``std``, when only the headers its prefixes name are. Hidden
directories, symlinked ones and CMake build trees are left unchecked.

Exits 0 when every include is allowed, 1 when any isn't or a file has no rules, and 2 when
include-rules.toml is missing or invalid. It uses Python's standard library alone, so it runs
without orbit.
"""

import re
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath

RULES_FILE = "include-rules.toml"
"""The rules' file, at the root."""

STD = "std"
"""The entry in a directory's rules that stops standard headers being allowed unless listed."""

SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"})
"""The suffixes of the C and C++ sources and headers checked."""

BUILD_TREE = "CMakeCache.txt"
"""The file marking a CMake build tree, whose generated and installed files are left unchecked."""

INCLUDE = re.compile(r'^\s*#\s*include\s*(<[^>\n]+>|"[^"\n]+")')
"""An ``#include`` line, its header as written, delimiters included, in the first group."""

C_HEADERS = frozenset(
    {
        "assert.h",
        "complex.h",
        "ctype.h",
        "errno.h",
        "fenv.h",
        "float.h",
        "inttypes.h",
        "iso646.h",
        "limits.h",
        "locale.h",
        "math.h",
        "setjmp.h",
        "signal.h",
        "stdalign.h",
        "stdarg.h",
        "stdatomic.h",
        "stdbit.h",
        "stdbool.h",
        "stdckdint.h",
        "stddef.h",
        "stdint.h",
        "stdio.h",
        "stdlib.h",
        "stdnoreturn.h",
        "string.h",
        "tgmath.h",
        "threads.h",
        "time.h",
        "uchar.h",
        "wchar.h",
        "wctype.h",
    }
)
"""C's standard headers."""

type Rules = Mapping[PurePosixPath, tuple[str, ...]]
"""The include prefixes each listed directory allows, by its path relative to the root."""


class RulesError(Exception):
    """Why include-rules.toml can't be used."""


def main(argv: Sequence[str]) -> int:
    """Check the includes under the root ``argv`` names after the program's name, or the current
    directory, print each violation, and return the exit status: 0 with none, 1 with any, and 2
    when the rules can't be used."""
    root = parse_root(argv)
    rules_path = root / RULES_FILE
    try:
        rules = read_rules(rules_path)
    except RulesError as error:
        print(f"{rules_path}: {error}", file=sys.stderr)
        return 2
    violations = find_violations(root, rules)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


def parse_root(argv: Sequence[str]) -> Path:
    """The root ``argv`` names after the program's name, or the current directory."""
    return Path(argv[1] if len(argv) > 1 else ".")


def find_violations(root: Path, rules: Rules) -> list[str]:
    """Each violation of ``rules`` in the files under ``root``, file by file."""
    return [line for path in find_sources(root) for line in check_file(root, path, rules)]


def read_rules(path: Path) -> Rules:
    """The rules in the file at ``path``.

    Raises :class:`RulesError` if it's missing, can't be read, or isn't valid rules.
    """
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RulesError("not found.") from None
    except OSError as error:
        raise RulesError(f"couldn't be read: {error.strerror}.") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise RulesError(f"isn't valid TOML: {error}.") from None
    return parse_rules(document)


def parse_rules(document: Mapping[str, object]) -> Rules:
    """The rules in the parsed TOML ``document``.

    Raises :class:`RulesError` if it has keys but ``rules``, or its ``rules`` isn't a table
    mapping directories under the root to lists of prefixes.
    """
    unknown = sorted(set(document) - {"rules"})
    if unknown:
        raise RulesError(f"has unknown keys: {', '.join(unknown)}.")
    table = document.get("rules")
    if not isinstance(table, dict):
        raise RulesError(
            "needs a [rules] table mapping each directory to its allowed include prefixes."
        )
    return {
        parse_directory(directory): parse_prefixes(directory, prefixes)
        for directory, prefixes in table.items()
    }


def parse_directory(directory: str) -> PurePosixPath:
    """The listed ``directory``'s path relative to the root.

    Raises :class:`RulesError` if it's absolute or climbs out of the root.
    """
    path = PurePosixPath(directory)
    if path.is_absolute() or ".." in path.parts:
        raise RulesError(f'"{directory}" isn\'t a directory under the root.')
    return path


def parse_prefixes(directory: str, prefixes: object) -> tuple[str, ...]:
    """The include prefixes ``prefixes`` lists for ``directory``, without trailing slashes.

    Raises :class:`RulesError` if it isn't a list of strings.
    """
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        raise RulesError(f'rules for "{directory}" must be a list of include prefixes.')
    return tuple(prefix.rstrip("/") for prefix in prefixes)


def find_sources(directory: Path) -> Iterator[Path]:
    """Each C and C++ source and header under ``directory``, sorted, but those in hidden
    directories, symlinked ones and CMake build trees."""
    for entry in list_visible(directory):
        if entry.is_dir():
            yield from find_sources(entry) if is_checked_directory(entry) else ()
        elif entry.suffix in SOURCE_SUFFIXES:
            yield entry


def list_visible(directory: Path) -> list[Path]:
    """What's in ``directory``, sorted, but what's hidden, whose name starts with a dot."""
    return sorted(entry for entry in directory.iterdir() if not entry.name.startswith("."))


def is_checked_directory(directory: Path) -> bool:
    """Whether ``directory`` is searched: neither a symlink nor a CMake build tree."""
    return not directory.is_symlink() and not (directory / BUILD_TREE).exists()


def check_file(root: Path, path: Path, rules: Rules) -> list[str]:
    """Each violation in the file at ``path`` under ``root``: an include its directory's rules
    don't allow, or the directory having no rules."""
    relative = PurePosixPath(path.relative_to(root).as_posix())
    listed = find_listed_directory(relative.parent, rules)
    if listed is None:
        return [f"{relative}: no rules cover {relative.parent}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        f"{relative}:{number}: {include} isn't allowed in {listed}"
        for number, include in find_includes(text)
        if not is_allowed(include, rules[listed])
    ]


def find_listed_directory(directory: PurePosixPath, rules: Rules) -> PurePosixPath | None:
    """The listed directory whose rules ``directory`` takes: itself or its nearest listed
    parent, or None when neither is listed."""
    candidates = (directory, *directory.parents)
    return next((candidate for candidate in candidates if candidate in rules), None)


def find_includes(text: str) -> Iterator[tuple[int, str]]:
    """Each include in ``text``: its line number and its header as written, with its
    delimiters."""
    for number, line in enumerate(text.splitlines(), start=1):
        match = INCLUDE.match(line)
        if match:
            yield number, match.group(1)


def is_allowed(include: str, prefixes: tuple[str, ...]) -> bool:
    """Whether ``prefixes`` allow ``include``, written with its delimiters: one names it or a
    folder above it, or it's a standard header and ``prefixes`` don't list ``std``."""
    header = include[1:-1]
    named = any(is_under(header, prefix) for prefix in prefixes)
    return named or (STD not in prefixes and is_standard(include))


def is_under(header: str, prefix: str) -> bool:
    """Whether ``header`` is ``prefix`` or under it, by whole path segments."""
    return header == prefix or header.startswith(f"{prefix}/")


def is_standard(include: str) -> bool:
    """Whether ``include``, written with its delimiters, is a standard header: one of C's, or
    angled with no folder and no extension."""
    header = include[1:-1]
    bare = include.startswith("<") and "/" not in header and not PurePosixPath(header).suffix
    return header in C_HEADERS or bare


if __name__ == "__main__":
    sys.exit(main(sys.argv))
