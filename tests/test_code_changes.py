"""Whether a landing changes code: a changed path under a mapped path, with a code extension, or
named as a dependency or build manifest, at any depth."""

from collections.abc import Sequence

import pytest

from orbit.agent.domain.code_changes import MANIFEST_NAMES, changes_code

MAPPED = ("src", "tests")
"""The paths a project's ``[[skills]]`` map, as Orbit's own do."""


@pytest.mark.parametrize(
    ("changed", "mapped", "code"),
    [
        (["docs/a.md", "README.md"], MAPPED, False),
        (["src/orbit/x.md"], MAPPED, True),
        (["tests"], MAPPED, True),
        (["srcs/x.md"], MAPPED, False),
        (["scripts/x.py"], MAPPED, True),
        (["pyproject.toml"], MAPPED, True),
        (["web/package.json"], MAPPED, True),
        (["requirements-dev.txt"], MAPPED, True),
        (["deps/requirements.txt"], MAPPED, True),
        (["requirements.md"], MAPPED, False),
        (["docs/a.md", "src/x.py"], MAPPED, True),
        (["docs/a.md", "scripts/x.py"], MAPPED, True),
        (["README.md", "web/package.json"], MAPPED, True),
        (["docs/a.md"], (), False),
        (["a.py"], (), True),
        (["docs/a.md"], (".",), True),
        ([], MAPPED, False),
    ],
)
def test_a_landing_changes_code_by_mapped_path_extension_or_manifest(
    changed: list[str], mapped: Sequence[str], code: bool
) -> None:
    assert changes_code(changed, mapped) is code


@pytest.mark.parametrize("name", sorted(MANIFEST_NAMES))
def test_every_manifest_changed_at_any_depth_is_code(name: str) -> None:
    assert changes_code([f"pkg/{name.replace('*', '-test')}"], MAPPED)
