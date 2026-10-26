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

from orbit.agent.external.whole_files import write_private
from orbit.protocol import ToolAccess

AGENTS_DIR = Path("~/.kiro/agents")
PREFIX = "orbit-"
"""What the names of orbit's agents, and their config files, start with."""
SERVER = "orbit"
"""The name orbit's MCP server goes by in a config."""
MAX_AGE = timedelta(days=1)
"""How old a config file gets before a sweep takes it for a leftover."""

# Kiro's V2 built-ins that read and search files, and neither write them nor run commands, from
# https://kiro.dev/docs/reference/built-in-tools/. Its code tool searches too, but also rewrites.
READ_ONLY_BUILTINS = ("read", "glob", "grep")
BUILTINS: dict[ToolAccess, tuple[str, ...]] = {
    "off": (),
    "restricted": READ_ONLY_BUILTINS,
    "full": ("@builtin",),
}


def kiro_agent_config(
    rules: str, instructions: str, mcp_url: str, token: str, tool_access: ToolAccess
) -> dict[str, Any]:
    """The config of a Kiro agent told orbit's ``rules`` and then its role's ``instructions``,
    connected to orbit's MCP server at ``mcp_url`` with ``token``, and to no other, allowed
    orbit's tools and the built-ins ``tool_access`` gives, all trusted up front."""
    tools = [f"@{SERVER}", *BUILTINS[tool_access]]
    headers = {"Authorization": f"Bearer {token}"}
    return {
        "prompt": f"{rules}\n\n{instructions}",
        "mcpServers": {SERVER: {"url": mcp_url, "headers": headers}},
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
