"""The tools the agent can call, looked up by name."""

from collections.abc import Iterable
from typing import Any

from orbit.agent.application.ports import Tool
from orbit.agent.domain.conversation import ToolSpec


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools = {tool.spec.name: tool for tool in tools}

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    async def run(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return await tool.run(arguments)
