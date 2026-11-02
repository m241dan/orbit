"""Workspace levels and web access in settings and agent entries: mapped from the tool access an
older peer sends, and sent with the tool access it reads."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from orbit.protocol import (
    AgentEntry,
    Settings,
    ToolAccess,
    WorkspaceLevel,
    levels_of_tool_access,
)

OLD_VALUES: list[tuple[ToolAccess, WorkspaceLevel, bool]] = [
    ("off", "none", False),
    ("restricted", "read", True),
    ("full", "shell", True),
]
"""Each tool access an older peer knows, with the workspace level and web access it means."""

LEVELS: list[tuple[WorkspaceLevel, ToolAccess]] = [
    ("none", "off"),
    ("read", "restricted"),
    ("write", "full"),
    ("shell", "full"),
]
"""Each workspace level, with the tool access an older peer knows it by."""


def test_restricted_tool_access_means_reading_with_web() -> None:
    settings = Settings(tool_access="restricted")
    assert (settings.workspace_level, settings.web) == ("read", True)


def test_the_default_settings_read_with_web() -> None:
    assert Settings() == Settings(workspace_level="read", web=True)
    assert Settings().tool_access == "restricted"


def test_a_write_level_is_sent_as_full_tool_access() -> None:
    sent = json.loads(Settings(workspace_level="write").model_dump_json())
    assert (sent["tool_access"], sent["workspace_level"], sent["web"]) == ("full", "write", True)


@pytest.mark.parametrize(("access", "level", "web"), OLD_VALUES)
def test_each_old_tool_access_round_trips_through_its_level(
    access: ToolAccess, level: WorkspaceLevel, web: bool
) -> None:
    settings = Settings(tool_access=access)
    read = Settings.model_validate_json(settings.model_dump_json())
    assert read == settings
    assert (read.tool_access, read.workspace_level, read.web) == (access, level, web)


@pytest.mark.parametrize(("access", "level", "web"), OLD_VALUES)
def test_an_older_peers_tool_access_alone_sets_the_level_and_web(
    access: ToolAccess, level: WorkspaceLevel, web: bool
) -> None:
    settings = Settings.model_validate({"tool_access": access}, context={"wire": True})
    assert (settings.workspace_level, settings.web) == (level, web)


@pytest.mark.parametrize(("level", "access"), LEVELS)
def test_each_level_is_sent_with_the_tool_access_older_peers_know_it_by(
    level: WorkspaceLevel, access: ToolAccess
) -> None:
    assert Settings(workspace_level=level).model_dump()["tool_access"] == access


def test_tool_access_given_with_web_keeps_the_web_given() -> None:
    settings = Settings(tool_access="full", web=False)
    assert (settings.workspace_level, settings.web) == ("shell", False)


def test_a_level_given_wins_over_the_tool_access_given_with_it() -> None:
    settings = Settings(tool_access="off", workspace_level="write")
    assert (settings.workspace_level, settings.tool_access) == ("write", "full")


@pytest.mark.parametrize(
    "data",
    [
        {"workspace_level": "everything"},
        {"workspace_level": ["read"]},
        {"tool_access": ["off"]},
        {"web": "sometimes"},
        "read",
    ],
)
def test_settings_refuse_what_isnt_a_level_tool_access_or_web(data: Any) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_settings_copied_with_another_level_are_still_sent_with_its_tool_access() -> None:
    copied = Settings().model_copy(update={"workspace_level": "none"})
    assert copied.model_dump()["tool_access"] == "off"


def test_updated_settings_keep_their_tool_access_agreeing_with_their_level() -> None:
    updated = Settings(model="opus").updated({"workspace_level": "shell", "web": False})
    assert updated == Settings(model="opus", workspace_level="shell", web=False)
    assert updated.tool_access == "full"


def test_unknown_tool_access_means_no_level() -> None:
    assert levels_of_tool_access("sometimes") is None
    assert levels_of_tool_access("full") == ("shell", True)


def test_an_agent_entry_from_an_older_peer_maps_its_tool_access() -> None:
    entry = AgentEntry.model_validate(
        {"name": "coder", "label": "Coder", "conversational": False, "tool_access": "off"},
        context={"wire": True},
    )
    assert (entry.workspace_level, entry.web) == ("none", False)


def test_an_agent_entry_is_sent_with_the_tool_access_of_its_level() -> None:
    entry = AgentEntry(name="coder", label="Coder", conversational=False, workspace_level="write")
    sent = json.loads(entry.model_dump_json())
    assert (sent["tool_access"], sent["workspace_level"], sent["web"]) == ("full", "write", True)
    assert AgentEntry.model_validate_json(entry.model_dump_json()) == entry
