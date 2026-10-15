"""Planning in conversation: the planner agent, whose proposals become draft tasks.

The planner is one agent for the whole session. Each prompt is a turn of its conversation,
resumed from the last, so it remembers what was said; it proposes tasks with ``propose_task``,
and they wait on the board as drafts until the user accepts them.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from orbit.agent.application.agent_run import AgentDesk
from orbit.agent.application.ports import AgentLaunch, Reply
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import TaskBoard
from orbit.protocol import (
    PLAN_TASK,
    AgentExited,
    AgentStarted,
    Event,
    ProgressNoted,
    Settings,
    TasksChanged,
    TaskSpec,
)


class PlannerChat:
    """The conversation with the planner, a turn per prompt."""

    def __init__(self, desk: AgentDesk, board: TaskBoard) -> None:
        self._desk = desk
        self._board = board
        self._agent: Agent | None = None
        self._tools: dict[str, Callable[[Agent, dict[str, Any]], Awaitable[Reply]]] = {
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
        launch = AgentLaunch(
            self._agent,
            await self._desk.url(),
            prompt,
            Settings(model=settings.model, tool_access=role.tool_access),
            ".",
            role.instructions,
            role.tools,
        )
        async for event in self._desk.agents.launcher.converse(turn_id, launch):
            yield event

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        answer = self._tools.get(tool)
        if answer is None or tool not in self._desk.agents.roles["planner"].tools:
            return Reply(f"Your role (planner) can't use {tool}.", is_error=True)
        assert self._agent is not None  # It called, so it was opened.
        return await answer(self._agent, arguments)

    async def close(self) -> None:
        """End the planner's conversation, at the session's end."""
        if self._agent is not None:
            self._desk.close(self._agent)
            await self._desk.publish(AgentExited(agent_id=self._agent.id, reason="finished"))

    async def _propose_task(self, agent: Agent, arguments: dict[str, Any]) -> Reply:
        title = str(arguments.get("title", "")).strip()
        prompt = str(arguments.get("prompt", "")).strip()
        coder = self._desk.agents.roles["coder"]
        task = TaskSpec(
            prompt=f"{title}\n\n{prompt}",
            settings=Settings(model=coder.model, tool_access=coder.tool_access),
        )
        task_id = self._board.add_draft(task)
        await self._desk.publish(TasksChanged(tasks=self._board.tasks))
        return Reply(f"Added draft task {task_id}; the user will accept or drop it.")

    async def _report_progress(self, agent: Agent, arguments: dict[str, Any]) -> Reply:
        note = str(arguments.get("note", "")).strip()
        await self._desk.publish(ProgressNoted(agent_id=agent.id, task_id=PLAN_TASK, note=note))
        return Reply("Noted.")
