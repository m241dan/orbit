"""Kiro's agent configs: what each agent runs with, and the files they're written to.

Kiro finds an agent's config by name among the files in ``~/.kiro/agents``, so each agent's is
written there under a name of its own run's, so no other run, workspace or project shares it,
readable by its owner alone as it holds the agent's token, just before the agent
starts, and removed once it ends. Configs a crash left behind are swept away a day later.
"""

import contextlib
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from orbit.agent.application.ports import AgentLaunch
from orbit.agent.domain.tools import orbit_tool_grant
from orbit.agent.external.whole_files import write_private
from orbit.protocol import Settings, WorkspaceLevel

AGENTS_DIR = Path("~/.kiro/agents")
PREFIX = "orbit-"
"""What the names of orbit's agents, and their config files, start with."""
SERVER = "orbit"
"""The name orbit's MCP server goes by in a config."""
MAX_AGE = timedelta(days=1)
"""How old a config file gets before a sweep takes it for a leftover."""

# Kiro's V2 built-ins, from https://kiro.dev/docs/reference/built-in-tools/. Those that read and
# search files, and neither write them nor run commands; its code tool searches too, but also
# rewrites.
READ_ONLY_BUILTINS = ("read", "glob", "grep")
WRITE_BUILTIN = "write"
"""The built-in that creates and edits files."""
WEB_BUILTINS = ("web_search", "web_fetch")
"""The built-ins that search the web and fetch from it."""
EVERY_BUILTIN = "@builtin"
"""What names every built-in, the web's included."""
NON_WEB_BUILTINS = (
    *READ_ONLY_BUILTINS,
    WRITE_BUILTIN,
    *("shell", "aws", "code", "introspect", "delegate", "subagent"),
)
"""The built-ins, apart from the web's and the experimental ones, that an agent at the workspace
level ``shell`` without web access is given in place of every built-in."""
LEVEL_BUILTINS: dict[WorkspaceLevel, tuple[str, ...]] = {
    "none": (),
    "read": READ_ONLY_BUILTINS,
    "write": (*READ_ONLY_BUILTINS, WRITE_BUILTIN),
    "shell": NON_WEB_BUILTINS,
}
"""The built-ins an agent at each workspace level is given, without the web's."""


def builtins(settings: Settings) -> tuple[str, ...]:
    """The built-ins an agent with ``settings`` is given: its workspace level's, and the web's
    if it has web access; at ``shell`` with web access, every one."""
    if settings.workspace_level == "shell" and settings.web:
        return (EVERY_BUILTIN,)
    level = LEVEL_BUILTINS[settings.workspace_level]
    return (*level, *WEB_BUILTINS) if settings.web else level


def kiro_tool_name(tool: str) -> str:
    """The name a config gives orbit's ``tool`` by."""
    # One MCP server's tool is @<server>/<tool>, from
    # https://kiro.dev/docs/custom-agents/configuration-reference/.
    return f"@{SERVER}/{tool}"


def kiro_agent_config(rules: str, launch: AgentLaunch) -> dict[str, Any]:
    """The config of ``launch``'s Kiro agent, told orbit's ``rules`` and then its role's
    instructions, connected to orbit's MCP server at the launch's URL with its agent's token, and
    to no other, allowed the orbit tools its role is granted and the built-ins its workspace
    level and web access give, all trusted up front."""
    orbit = [kiro_tool_name(tool) for tool in orbit_tool_grant(launch.tools).granted]
    tools = [*orbit, *builtins(launch.settings)]
    headers = {"Authorization": f"Bearer {launch.agent.token}"}
    return {
        "prompt": f"{rules}\n\n{launch.instructions}",
        "mcpServers": {SERVER: {"url": launch.url, "headers": headers}},
        "includeMcpJson": False,
        "tools": tools,
        "allowedTools": tools,
    }


class KiroAgentFiles:
    """The config files of orbit's Kiro agents in ``agents_dir``."""

    def __init__(self, agents_dir: Path = AGENTS_DIR) -> None:
        self.agents_dir = agents_dir.expanduser()

    def write(self, agent_id: str, config: dict[str, Any]) -> str:
        """Write ``config`` as agent ``agent_id``'s, owner-only, under a name no other run
        shares, and return that name, which Kiro knows it by. Raises ``OSError`` when it can't."""
        # Agent ids repeat across workspaces, and a guessable name lets a project shadow it.
        name = f"{PREFIX}{agent_id}-{secrets.token_hex(8)}"
        write_private(self._path(name), json.dumps({"name": name, **config}, indent=2) + "\n")
        return name

    def remove(self, name: str) -> None:
        """Remove the config written under ``name``, if there is one."""
        self._path(name).unlink(missing_ok=True)

    def sweep(self, now: datetime) -> None:
        """Remove the configs of orbit's agents last written more than a day before ``now``."""
        cutoff = (now - MAX_AGE).timestamp()
        for path in self.agents_dir.glob(f"{PREFIX}*.json"):
            # Another orbit may sweep the same directory at once.
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    def _path(self, name: str) -> Path:
        return self.agents_dir / f"{name}.json"
