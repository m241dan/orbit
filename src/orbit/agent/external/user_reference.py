"""Orbit's reference user configuration: every setting the user's own ``config.toml`` in the
config directory can change, commented out at its default under a line saying what it does,
generated from orbit's models, with examples of the tables it may add.

Run as ``python -m orbit.agent.external.user_reference [path]`` to write it, by default to
``USER_REFERENCE_PATH``, the copy shipped with orbit.
"""

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orbit.agent.domain.config_update import FOR_EXAMPLE
from orbit.agent.domain.layers import RoleFields
from orbit.agent.domain.mcp_servers import McpServer
from orbit.agent.domain.remotes import RemoteSettings
from orbit.agent.domain.roles import Role
from orbit.agent.domain.server import ServerSettings
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.external.toml_comments import (
    comments,
    described,
    example_table,
    field_descriptions,
    key_line,
)
from orbit.telemetry.settings import TelemetrySettings

USER_REFERENCE_PATH = Path("src/orbit/agent/external/reference/user-config.toml")
"""Where the user reference is written, relative to the current directory: the copy shipped
with orbit."""

USER_HEADER = (
    "Your own orbit configuration, every setting commented out at its default.",
    "Uncomment the settings you change; the others keep their defaults.",
)

TABLES: tuple[tuple[str, type[BaseModel]], ...] = (
    ("storage", StorageSettings),
    ("server", ServerSettings),
    ("telemetry", TelemetrySettings),
    ("roles", RoleFields),
)
"""The user config's tables of settings, each by its header and the model of its fields."""

EXAMPLE_ROLE_MODEL = RoleFields(model="opus")
"""The example of a built-in role's machine fields set for that role alone."""
EXAMPLE_PERSONAL_ROLE = "helper"
EXAMPLE_PERSONAL_FIELDS = Role(
    instructions="roles/helper.md",
    lane="helper",
    workspace="snapshot",
    merge=False,
    verify=False,
)
"""The example of a personal role: just the fields it sets."""
PERSONAL_DESCRIPTIONS = {
    **field_descriptions(Role),
    "instructions": "What its agents are told about their job: the text, or a file in the config"
    " directory it names.",
}
"""The descriptions of a personal role's fields: a project role's, but its instructions file
read from the config directory."""
EXAMPLE_RUNTIME = "claude"
EXAMPLE_ALIAS = ("fast", "claude-haiku-5-5")
"""The example of an alias added to a runtime's models, and the id it resolves to."""
EXAMPLE_REMOTE = RemoteSettings(url="wss://orbit.example.com:7433", ca="certs/ca.pem")
"""The example of a control plane to join by name."""
EXAMPLE_SERVER = McpServer(command=("/opt/bin/github-mcp", "stdio"))
"""The example of a project's MCP server pointed at this machine's copy."""


def render_user_reference() -> str:
    """The user reference config: a short header; the ``[storage]``, ``[server]``,
    ``[telemetry]`` and ``[roles]`` tables with each field at its default, unset if it has
    none; then examples of a built-in role's model, a personal role, an alias of a runtime's
    models, a remote and an MCP server override. Everything is commented out, each key under
    its description.
    """
    sections = [
        comments(USER_HEADER),
        *(_render_table(header, model) for header, model in TABLES),
        example_table(
            "A built-in role's model, effort or runtime, for that role alone.",
            "[roles.planner]",
            EXAMPLE_ROLE_MODEL,
        ),
        example_table(
            "A personal role, its instructions a file beside this one or the text.",
            f"[roles.{EXAMPLE_PERSONAL_ROLE}]",
            EXAMPLE_PERSONAL_FIELDS,
            PERSONAL_DESCRIPTIONS,
        ),
        _render_alias_example(),
        example_table(
            "A control plane orbit chat --remote work joins.",
            "[remotes.work]",
            EXAMPLE_REMOTE,
        ),
        example_table(
            "A project's MCP server pointed at this machine's copy.",
            "[mcp.github]",
            EXAMPLE_SERVER,
        ),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def write_user_reference(path: Path) -> None:
    """Writes the user reference config to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_user_reference(), encoding="utf-8")


def _render_table(header: str, model: type[BaseModel]) -> list[str]:
    """The table under ``header``, each of ``model``'s fields at its default under its
    description, unset if it has none."""
    defaults: Mapping[str, Any] = model().model_dump(mode="json")
    return [f"# [{header}]", *described(defaults, field_descriptions(model))]


def _render_alias_example() -> list[str]:
    """The example of an alias added to a runtime's models, under its description."""
    alias, model = EXAMPLE_ALIAS
    return [
        f"# Your own aliases of a runtime's models, or what one resolves to. {FOR_EXAMPLE}",
        f"# [runtimes.{EXAMPLE_RUNTIME}.models]",
        "# An alias, giving the model id it resolves to.",
        key_line(alias, model),
    ]


if __name__ == "__main__":
    write_user_reference(Path(sys.argv[1]) if len(sys.argv) > 1 else USER_REFERENCE_PATH)
