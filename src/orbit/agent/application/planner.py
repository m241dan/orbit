"""Planning in conversation: the planner agent, whose proposals become draft tasks.

The planner is one agent for the whole session. Each prompt is a turn of its conversation,
resumed from the last, so it remembers what was said; it proposes tasks with ``propose_task``,
and they wait on the board as drafts until the user accepts them.
"""

from collections.abc import AsyncIterator
from typing import Any

from orbit.agent.application.agent_run import AgentDesk, Tool, dispatch, report_progress
from orbit.agent.application.ports import Reply
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import suggested_task
from orbit.protocol import (
    PLAN_TASK,
    AgentExited,
    AgentStarted,
    Event,
    Settings,
    TasksChanged,
)


class PlannerChat:
    """The conversation with the planner, a turn per prompt."""

    def __init__(self, desk: AgentDesk, board: TaskBoard) -> None:
        self._desk = desk
        self._board = board
        self._agent: Agent | None = None
        self._tools: dict[str, Tool] = {
            "propose_task": self._propose_task,
            "report_progress": self._report_progress,
        }

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        """Tell the planner ``prompt``, with the session's model, and yield what it does."""
        if self._agent is None:
            self._agent = self._desk.open(self, "planner", PLAN_TASK)
            # The frontend learns from this which agent's output is the planner's.
            await self._desk.publish(
                AgentStarted(agent_id=self._agent.id, role="planner", task_id=PLAN_TASK)
            )
        role = self._desk.agents.roles["planner"]
        own = Settings(model=settings.model, tool_access=role.tool_access)
        launch = await self._desk.launch(self._agent, prompt, own, ".")
        async for event in self._desk.agents.launcher.converse(turn_id, launch):
            yield event

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        role = self._desk.agents.roles["planner"]
        return await dispatch("planner", role, self._tools, tool, arguments)

    async def close(self) -> None:
        """End the planner's conversation, at the session's end."""
        if self._agent is not None:
            self._desk.close(self._agent)
            await self._desk.publish(AgentExited(agent_id=self._agent.id, reason="finished"))

    async def _propose_task(self, arguments: dict[str, Any]) -> Reply:
        title, prompt = str(arguments.get("title", "")), str(arguments.get("prompt", ""))
        task = suggested_task(self._desk.agents.roles["coder"], title, prompt)
        task_id = self._board.add_draft(task)
        await self._desk.publish(TasksChanged(tasks=self._board.tasks))
        return Reply(f"Added draft task {task_id}; the user will accept or drop it.")

    async def _report_progress(self, arguments: dict[str, Any]) -> Reply:
        assert self._agent is not None  # It called, so it was opened.
        return await report_progress(self._desk.publish, self._agent.id, PLAN_TASK, arguments)
