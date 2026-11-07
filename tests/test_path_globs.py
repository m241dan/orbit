"""Globs over git paths: ``*`` stays within one directory, and ``**`` crosses any number of
them, none included."""

import pytest

from orbit.agent.domain.path_globs import matches_path


@pytest.mark.parametrize(
    ("pattern", "path", "matched"),
    [
        ("pyproject.toml", "pyproject.toml", True),
        ("pyproject.toml", "sub/pyproject.toml", False),
        ("*.md", "README.md", True),
        ("*.md", "docs/plan.md", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/pkg/a.py", False),
        ("**/git.py", "git.py", True),
        ("**/git.py", "src/orbit/git.py", True),
        ("**/git.py", "src/orbit/git.pyc", False),
        ("src/**/external/*.py", "src/external/git.py", True),
        ("src/**/external/*.py", "src/orbit/agent/external/git.py", True),
        ("src/**/external/*.py", "src/orbit/external/sub/git.py", False),
        ("src/**/external/*.py", "tests/external/git.py", False),
        ("src/**", "src/orbit/agent/external/git.py", True),
        ("src/**", "tests/test_git.py", False),
        ("src/**/external/**", "src/orbit/agent/external/git.py", True),
        ("src/**/external/**", "src/orbit/agent/domain/git.py", False),
    ],
)
def test_a_path_matches_a_glob_whose_star_stays_in_a_directory_and_double_star_crosses_them(
    pattern: str, path: str, matched: bool
) -> None:
    assert matches_path(pattern, path) is matched
