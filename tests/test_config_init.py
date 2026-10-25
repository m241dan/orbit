"""Starting a project's ``.orbit/config.toml`` from the reference config: written where there's
none, refused over one, and with ``force`` replacing it after keeping it as a numbered backup;
and bringing one up to date with it, adding what it doesn't mention."""

from datetime import date
from pathlib import Path

import pytest

from orbit.agent.domain.config import LIMIT_KEYS
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.config_update import Additions
from orbit.agent.domain.roles import Role
from orbit.agent.external.config_file import CONFIG_PATH
from orbit.agent.external.config_init import (
    REFERENCE,
    ConfigExists,
    InitResult,
    UpdateResult,
    init_config,
    update_config,
)
from orbit.agent.external.reference_config import reference_config

TODAY = date(2026, 10, 25)

ADDED = "# Added by orbit init --update on 2026-10-25: settings this file didn't mention."

CODER_OPUS = '[roles.coder]\nmodel = "opus"\n'

OWN = """\
# Our configuration.

# Quick checks first.
[[verification.steps]]
name = "Lint"
command = ["ruff", "check"]

[[verification.steps]]
name = "Tests"
command = ["pytest"]

# Keep the environment in step with the lock file.
[setup]
command = ["uv", "sync"]
watch = ["uv.lock"]

[roles.coder]
model = "opus"
merge = false
"""


def write(root: Path, text: str) -> Path:
    """The project file of the workspace at ``root``, holding ``text``."""
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_the_shipped_reference_is_the_reference_config() -> None:
    assert REFERENCE.read_text(encoding="utf-8") == reference_config()


def test_without_a_file_the_reference_is_written_in_a_new_orbit_directory(tmp_path: Path) -> None:
    result = init_config(tmp_path, force=False)
    path = tmp_path / CONFIG_PATH
    assert result == InitResult(path)
    assert path.read_text(encoding="utf-8") == reference_config()
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_an_existing_file_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    with pytest.raises(ConfigExists) as raised:
        init_config(tmp_path, force=False)
    assert raised.value.path == path
    assert path.read_text() == "fix_attempts = 3\n"
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_force_keeps_the_existing_file_as_the_first_backup(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    result = init_config(tmp_path, force=True)
    backup = path.with_name("config.toml.bak.1")
    assert result == InitResult(path, backup)
    assert backup.read_text() == "fix_attempts = 3\n"
    assert path.read_text(encoding="utf-8") == reference_config()


def test_force_twice_keeps_both_earlier_files(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    init_config(tmp_path, force=True)
    result = init_config(tmp_path, force=True)
    second = path.with_name("config.toml.bak.2")
    assert result == InitResult(path, second)
    assert path.with_name("config.toml.bak.1").read_text() == "fix_attempts = 3\n"
    assert second.read_text(encoding="utf-8") == reference_config()
    assert path.read_text(encoding="utf-8") == reference_config()


def test_force_takes_the_first_backup_number_that_is_free(tmp_path: Path) -> None:
    path = write(tmp_path, "fix_attempts = 3\n")
    path.with_name("config.toml.bak.1").write_text("older\n")
    result = init_config(tmp_path, force=True)
    second = path.with_name("config.toml.bak.2")
    assert result == InitResult(path, second)
    assert second.read_text() == "fix_attempts = 3\n"
    assert path.with_name("config.toml.bak.1").read_text() == "older\n"


def test_force_without_a_file_writes_the_reference_with_no_backup(tmp_path: Path) -> None:
    result = init_config(tmp_path, force=True)
    path = tmp_path / CONFIG_PATH
    assert result == InitResult(path)
    assert path.read_text(encoding="utf-8") == reference_config()
    assert [p.name for p in path.parent.iterdir()] == ["config.toml"]


def test_an_orbit_directory_that_is_a_file_raises(tmp_path: Path) -> None:
    (tmp_path / ".orbit").write_text("")
    with pytest.raises(OSError):
        init_config(tmp_path, force=False)


def update(root: Path) -> UpdateResult:
    """The project file of the workspace at ``root`` updated on :data:`TODAY`."""
    return update_config(root, today=lambda: TODAY)


def block_starting(blocks: list[str], start: str) -> str:
    """The one of ``blocks`` that starts with ``start``."""
    [found] = [block for block in blocks if block.startswith(start)]
    return found


def fields_but_model(coder: str) -> list[str]:
    """The lines of the reference's block ``coder`` under its header but those of its model."""
    _, *lines = coder.split("\n")
    pairs = zip(lines[::2], lines[1::2], strict=True)
    return [line for pair in pairs if not pair[1].startswith("# model") for line in pair]


def test_update_without_a_file_writes_the_reference(tmp_path: Path) -> None:
    result = update(tmp_path)
    path = tmp_path / CONFIG_PATH
    assert result == UpdateResult(path)
    assert path.read_text(encoding="utf-8") == reference_config()


def test_update_adds_a_roles_other_fields_after_its_line_and_the_other_tables_at_the_end(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, CODER_OPUS)
    update(tmp_path)
    # The reference's blocks: its header, the limits, the example step and setup, the line
    # introducing the roles, then a block per role and per later-version setting.
    _, limits, *rest = reference_config().removesuffix("\n").split("\n\n")
    roles_line = block_starting(rest, "# The roles agents work in")
    coder = block_starting(rest, "# [roles.coder]")
    appended = "\n\n".join(block for block in rest if block not in (roles_line, coder))
    fields = fields_but_model(coder)
    expected = [limits, "", *CODER_OPUS.splitlines(), *fields, "", ADDED, "", appended]
    assert path.read_text(encoding="utf-8") == "\n".join(expected) + "\n"


def test_update_says_what_it_added_where(tmp_path: Path) -> None:
    path = write(tmp_path, CODER_OPUS)
    assert update(tmp_path) == UpdateResult(
        path,
        Additions(
            keys={
                "": LIMIT_KEYS,
                "[roles.coder]": tuple(key for key in Role.model_fields if key != "model"),
            },
            tables=(
                "[[verification.steps]]",
                "[setup]",
                "[roles.planner]",
                "[roles.reviewer]",
                "[roles.super-reviewer]",
                "[roles.configurator]",
                "[roles.security-reviewer]",
                "[triggers.review-each-commit]",
                "[mcp.github]",
                "[mcp.semgrep]",
                "[roles.security-reviewer.tools]",
                "[models.local-coder]",
            ),
        ),
    )


def test_updating_again_adds_nothing_and_leaves_the_file_alone(tmp_path: Path) -> None:
    path = write(tmp_path, CODER_OPUS)
    update(tmp_path)
    text = path.read_text(encoding="utf-8")
    inode = path.stat().st_ino
    assert update(tmp_path) == UpdateResult(path, Additions())
    assert path.read_text(encoding="utf-8") == text
    assert path.stat().st_ino == inode


def test_update_keeps_every_line_of_the_file_unchanged_and_in_order(tmp_path: Path) -> None:
    path = write(tmp_path, OWN)
    update(tmp_path)
    lines = iter(path.read_text(encoding="utf-8").splitlines())
    assert all(line in lines for line in OWN.splitlines())


def test_update_doesnt_add_a_key_its_table_has_commented_out(tmp_path: Path) -> None:
    path = write(tmp_path, f"{CODER_OPUS}# merge = true\n")
    result = update(tmp_path)
    assert result.additions is not None
    assert "merge" not in result.additions.keys["[roles.coder]"]
    assert path.read_text(encoding="utf-8").count("# merge = true") == 1


def test_update_refuses_a_file_that_isnt_toml_naming_it(tmp_path: Path) -> None:
    path = write(tmp_path, "[roles.coder\n")
    with pytest.raises(ConfigError) as raised:
        update(tmp_path)
    assert str(raised.value).startswith(f"{path}: ")
    assert path.read_text() == "[roles.coder\n"


def test_update_refuses_a_file_that_isnt_utf8_naming_it(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    path.write_bytes(b"\xff\n")
    with pytest.raises(ConfigError) as raised:
        update(tmp_path)
    assert str(raised.value).startswith(f"{path}: ")
    assert path.read_bytes() == b"\xff\n"


def test_update_raises_when_it_cant_read_the_file(tmp_path: Path) -> None:
    (tmp_path / ".orbit").write_text("")
    with pytest.raises(OSError):
        update(tmp_path)
