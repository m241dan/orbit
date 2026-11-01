"""The repository's CLAUDE.md: a pointer to the skill files that hold Orbit's conventions, each
of which exists."""

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
SKILL_FILES = (
    "src/orbit/agent/external/skills/code-style/SKILL.md",
    "src/orbit/agent/external/skills/python/SKILL.md",
    "src/orbit/agent/external/skills/application/SKILL.md",
    ".orbit/skills/layers/SKILL.md",
    ".orbit/skills/orbit/SKILL.md",
)
"""The skill files CLAUDE.md tells a session to read, relative to the repository's root."""


@pytest.mark.parametrize("path", SKILL_FILES)
def test_claude_md_names_the_skill_file(path: str) -> None:
    assert f"`{path}`" in CLAUDE_MD.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", SKILL_FILES)
def test_the_skill_file_claude_md_names_exists(path: str) -> None:
    assert (ROOT / path).is_file()
