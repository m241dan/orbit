"""Kiro's agent configs: what one holds for each tool access, and the owner-only files they're
written to under ``~/.kiro/agents``, removed after their run or, left over, a day later."""

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orbit.agent.external.kiro_agents import (
    READ_ONLY_BUILTINS,
    KiroAgentFiles,
    kiro_agent_config,
)
from orbit.protocol import ToolAccess

URL = "http://127.0.0.1:4321/mcp"
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def config(tool_access: ToolAccess = "off") -> dict[str, Any]:
    return kiro_agent_config("The rules.", "The role's instructions.", URL, "secret", tool_access)


def aged(path: Path, age: timedelta) -> Path:
    """``path``, written with ``{}`` and last modified ``age`` before ``NOW``."""
    path.write_text("{}")
    modified = (NOW - age).timestamp()
    os.utime(path, (modified, modified))
    return path


def test_a_config_gives_only_orbits_tools_with_tool_access_off() -> None:
    assert config("off") == {
        "prompt": "The rules.\n\nThe role's instructions.",
        "mcpServers": {"orbit": {"url": URL, "headers": {"Authorization": "Bearer secret"}}},
        "includeMcpJson": False,
        "tools": ["@orbit"],
        "allowedTools": ["@orbit"],
    }


def test_restricted_access_adds_the_read_only_built_ins_pre_trusted() -> None:
    restricted = config("restricted")
    assert restricted["tools"] == ["@orbit", *READ_ONLY_BUILTINS]
    assert restricted["allowedTools"] == restricted["tools"]


def test_the_read_only_built_ins_neither_write_nor_run_commands() -> None:
    assert READ_ONLY_BUILTINS == ("read", "glob", "grep")


def test_full_access_adds_every_built_in_pre_trusted() -> None:
    full = config("full")
    assert full["tools"] == ["@orbit", "@builtin"]
    assert full["allowedTools"] == full["tools"]


def test_the_agents_live_in_kiros_home_directory() -> None:
    assert KiroAgentFiles().agents_dir == Path.home() / ".kiro" / "agents"


def test_a_config_is_written_owner_only_under_the_agents_name(tmp_path: Path) -> None:
    name = KiroAgentFiles(tmp_path).write("agent-7", config())
    path = tmp_path / f"{name}.json"
    assert re.fullmatch(r"orbit-agent-7-[0-9a-f]{16}", name)
    assert json.loads(path.read_text()) == {"name": name, **config()}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writing_makes_the_agents_directory_owner_only(tmp_path: Path) -> None:
    agents = tmp_path / ".kiro" / "agents"
    KiroAgentFiles(agents).write("agent-7", config())
    assert stat.S_IMODE(agents.stat().st_mode) == 0o700


def test_a_written_config_once_removed_leaves_no_file(tmp_path: Path) -> None:
    files = KiroAgentFiles(tmp_path)
    files.remove(files.write("agent-7", config()))
    assert list(tmp_path.iterdir()) == []


def test_each_write_of_one_agents_config_gets_a_file_of_its_own(tmp_path: Path) -> None:
    files = KiroAgentFiles(tmp_path)
    first = files.write("agent-7", config())
    second = files.write("agent-7", config())
    assert first != second
    files.remove(first)
    assert [path.name for path in tmp_path.iterdir()] == [f"{second}.json"]


def test_removing_a_config_never_written_does_nothing(tmp_path: Path) -> None:
    KiroAgentFiles(tmp_path).remove("orbit-agent-7-0123456789abcdef")
    assert list(tmp_path.iterdir()) == []


def test_sweeping_removes_orbits_configs_older_than_a_day(tmp_path: Path) -> None:
    aged(tmp_path / "orbit-agent-7-0123456789abcdef.json", timedelta(days=2))
    KiroAgentFiles(tmp_path).sweep(NOW)
    assert list(tmp_path.iterdir()) == []


def test_sweeping_keeps_recent_configs_and_others(tmp_path: Path) -> None:
    kept = {
        aged(tmp_path / "orbit-new.json", timedelta(hours=1)),
        aged(tmp_path / "mine.json", timedelta(days=2)),
        aged(tmp_path / "orbit-notes.txt", timedelta(days=2)),
    }
    KiroAgentFiles(tmp_path).sweep(NOW)
    assert set(tmp_path.iterdir()) == kept


def test_sweeping_skips_a_config_gone_before_it_is_looked_at(tmp_path: Path) -> None:
    (tmp_path / "orbit-gone.json").symlink_to(tmp_path / "nowhere")
    aged(tmp_path / "orbit-agent-7-0123456789abcdef.json", timedelta(days=2))
    KiroAgentFiles(tmp_path).sweep(NOW)
    assert [path.name for path in tmp_path.iterdir()] == ["orbit-gone.json"]


def test_sweeping_without_an_agents_directory_does_nothing(tmp_path: Path) -> None:
    KiroAgentFiles(tmp_path / "missing").sweep(NOW)
    assert list(tmp_path.iterdir()) == []
