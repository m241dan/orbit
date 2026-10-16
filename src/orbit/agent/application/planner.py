"""Planning in conversation: the planner agent, whose proposals become draft tasks.

The planner is one agent for the whole session. Each prompt is a turn of its conversation,
resumed from the last, so it remembers what was said; it proposes tasks with ``propose_task``,
and they wait on the board as drafts until the user accepts them. Its first prompt, and the first
after the verification steps change, tells it the steps each task must pass.
"""

from collections.abc import AsyncIterator, Callable
from typing import Any

from orbit.agent.application.desk import AgentDesk, Tool, dispatch, report_progress
from orbit.agent.application.ports import Reply
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.roles import suggested_task
from orbit.agent.domain.verification import steps_note
from orbit.protocol import (
    CODER,
    PLAN_TASK,
    PLANNER,
    AgentExited,
    AgentStarted,
    Event,
    Settings,
    TasksChanged,
    TurnFinished,
)


class PlannerChat:
    """The conversation with the planner, a turn per prompt; ``step_names`` tells the names of
    the verification steps tasks must pass now."""

    def __init__(
        self, desk: AgentDesk, board: TaskBoard, step_names: Callable[[], tuple[str, ...]]
    ) -> None:
        self._desk = desk
        self._board = board
        self._step_names = step_names
        self._agent: Agent | None = None
        self._told: tuple[str, ...] | None = None  # The steps the planner last heard about.
        self._tools: dict[str, Tool] = {
            "propose_task": self._propose_task,
            "report_progress": self._report_progress,
        }

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        """Tell the planner ``prompt``, with the session's model, and yield what it does."""
        if self._agent is None:
            self._agent = self._desk.open(self, PLANNER, PLAN_TASK)
            # The frontend learns from this which agent's output is the planner's.
            await self._desk.publish(
                AgentStarted(agent_id=self._agent.id, role=PLANNER, task_id=PLAN_TASK)
            )
        role = self._desk.agents.roles[PLANNER]
        own = Settings(model=settings.model, tool_access=role.tool_access)
        steps = self._step_names()
        launch = await self._desk.launch(self._agent, self._with_steps(prompt, steps), own, ".")
        async for event in self._desk.agents.launcher.converse(turn_id, launch):
            # Only a completed turn surely delivered the note; otherwise the next prompt repeats it.
            if isinstance(event, TurnFinished) and event.reason == "completed":
                self._told = steps
            yield event

    def _with_steps(self, prompt: str, steps: tuple[str, ...]) -> str:
        """``prompt``, after a note of ``steps`` if the planner hasn't heard them."""
        if steps == self._told:
            return prompt
        return f"{steps_note(steps)}\n\n{prompt}"

    async def handle(self, tool: str, arguments: dict[str, Any]) -> Reply:
        role = self._desk.agents.roles[PLANNER]
        return await dispatch(PLANNER, role, self._tools, tool, arguments)

    async def close(self) -> None:
        """End the planner's conversation, at the session's end."""
        if self._agent is not None:
            self._desk.close(self._agent)
            await self._desk.publish(AgentExited(agent_id=self._agent.id, reason="finished"))

    async def _propose_task(self, arguments: dict[str, Any]) -> Reply:
        title, prompt = str(arguments.get("title", "")), str(arguments.get("prompt", ""))
        task = suggested_task(self._desk.agents.roles[CODER], title, prompt)
        task_id = self._board.add_draft(task)
        await self._desk.publish(TasksChanged(tasks=self._board.tasks))
        return Reply(f"Added draft task {task_id}; the user will accept or drop it.")

    async def _report_progress(self, arguments: dict[str, Any]) -> Reply:
        assert self._agent is not None  # It called, so it was opened.
        return await report_progress(self._desk.publish, self._agent.id, PLAN_TASK, arguments)
