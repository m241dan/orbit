"""Whether a landing changes code: a changed path under one of the project's mapped paths, its
``[[skills]]`` entries', a file with a code extension, or a dependency or build manifest, matched
by its name at any depth."""

import posixpath
from collections.abc import Collection, Sequence
from fnmatch import fnmatchcase

from orbit.agent.domain.mapped_paths import is_code_file, is_mapped

MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.cfg",
        "requirements*.txt",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "CMakeLists.txt",
        "CMakePresets.json",
        "vcpkg.json",
        "conanfile.txt",
        "meson.build",
        "Makefile",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "Gemfile.lock",
        "Dockerfile",
        "Containerfile",
    }
)
"""The names of the dependency and build manifests, a pattern for ``requirements*.txt``."""


def changes_code(changed: Collection[str], mapped_paths: Sequence[str]) -> bool:
    """Whether one of the paths ``changed`` is code: under one of ``mapped_paths``, when there
    are any, a file with an extension of code, or a manifest, by its name."""
    return any(_is_code(path, mapped_paths) for path in changed)


def _is_code(path: str, mapped_paths: Sequence[str]) -> bool:
    """Whether ``path`` is code: under one of ``mapped_paths``, if there are any, a file with an
    extension of code, or a manifest."""
    mapped = bool(mapped_paths) and is_mapped(path, mapped_paths)
    return mapped or is_code_file(path) or _is_manifest(path)


def _is_manifest(path: str) -> bool:
    """Whether ``path`` is a dependency or build manifest, by its name."""
    name = posixpath.basename(path)
    return any(fnmatchcase(name, pattern) for pattern in MANIFEST_NAMES)
