"""Planning in conversation: the planner agent, whose proposals become draft tasks.

The planner is one agent for the whole session. Each prompt is a turn of its conversation,
resumed from the last, so it remembers what was said; it proposes tasks with ``propose_task``,
and they wait on the board as drafts until the user accepts them. Its first prompt, and the first
after the verification steps change, tells it the steps each task must pass. A restored session
carries on its saved conversation, or starts a fresh one if that can't be resumed.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from orbit.agent.application.desk import AgentDesk, Tool, dispatch, report_progress
from orbit.agent.application.ports import Reply, ResumeFailed
from orbit.agent.application.tasks import Tasks
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.roles import suggested_task
from orbit.agent.domain.verification import steps_note
from orbit.protocol import (
    CODER,
    PLAN_TASK,
    PLANNER,
    AgentExited,
    AgentStarted,
    ErrorOccurred,
    Event,
    Notice,
    Settings,
    TurnEndReason,
    TurnFinished,
    TurnStarted,
)

RESUME_FAILED_NOTE = "Couldn't resume the planner's conversation; starting fresh."


class PlannerChat:
    """The conversation with the planner, a turn per prompt; ``step_names`` tells the names of
    the verification steps tasks must pass now."""

    def __init__(
        self, desk: AgentDesk, tasks: Tasks, step_names: Callable[[], tuple[str, ...]]
    ) -> None:
        self._desk = desk
        self._tasks = tasks
        self._board = tasks.board
        self._step_names = step_names
        self._agent: Agent | None = None
        self._told: tuple[str, ...] | None = None  # The steps the planner last heard about.
        self._saved: str | None = None  # The saved conversation, until a turn resumes it.
        self._tools: dict[str, Tool] = {
            "propose_task": self._propose_task,
            "report_progress": self._report_progress,
        }

    @property
    def agent_id(self) -> str | None:
        """The id of the planner's agent; none before its first turn opens it."""
        return self._agent.id if self._agent else None

    @property
    def conversation_id(self) -> str | None:
        """The id of the planner's Claude conversation, which may change after each turn; the
        restored one before the first turn, and otherwise none until a turn starts it."""
        if self._agent is None:
            return self._saved
        return self._desk.agents.launcher.conversation(self._agent.id)

    @property
    def started(self) -> bool:
        """Whether the planner has a conversation to end: one restored, or one its first turn
        opened."""
        return self._agent is not None or self._saved is not None

    def restore(self, conversation_id: str) -> None:
        """Carry on the saved conversation ``conversation_id`` from the first turn on, which
        starts a fresh one instead if it can't be resumed."""
        self._saved = conversation_id

    async def answer(self, text: str, settings: Settings) -> TurnEndReason:
        """Run a turn of the planner's on ``text``, publishing its events; returns how it ended.

        A turn cancelled before its TurnFinished is out ends as cancelled rather than raising,
        and one that fails then ends in error. Once its TurnFinished is out, a cancelled or
        failed turn returns that TurnFinished's reason without publishing another; a failure is
        still reported with ErrorOccurred.
        """
        turn_id = uuid.uuid4().hex
        finished: TurnEndReason | None = None  # How it ended, once its TurnFinished is out.
        try:
            async for event in self.run_turn(turn_id, text, settings):
                await self._desk.publish(event)
                if isinstance(event, TurnFinished):
                    finished = event.reason
        except asyncio.CancelledError:
            return await self._end(turn_id, finished, "cancelled")
        except Exception as exc:
            await self._desk.publish(
                ErrorOccurred(turn_id=turn_id, agent_id=self.agent_id, message=str(exc))
            )
            return await self._end(turn_id, finished, "error")
        return finished or "completed"

    async def _end(
        self, turn_id: str, finished: TurnEndReason | None, reason: TurnEndReason
    ) -> TurnEndReason:
        """How turn ``turn_id`` ended: ``finished`` if its TurnFinished was published, otherwise
        ``reason``, publishing a TurnFinished for it."""
        if finished is not None:
            return finished
        await self._desk.publish(
            TurnFinished(turn_id=turn_id, agent_id=self.agent_id, reason=reason)
        )
        return reason

    async def run_turn(self, turn_id: str, prompt: str, settings: Settings) -> AsyncIterator[Event]:
        """Tell the planner ``prompt``, with the session's model, and yield what it does. If it
        can't resume a saved conversation, the prompt goes to a fresh one, with a Notice."""
        await self._open()
        saved, self._saved = self._saved, None
        try:
            async for event in self._converse(turn_id, prompt, settings):
                yield event
        except ResumeFailed:
            if saved is None:
                raise
            async for event in self._start_fresh(turn_id, prompt, settings):
                yield event
        except asyncio.CancelledError:
            self._saved = saved  # It may not have reached the saved conversation yet.
            raise

    async def _open(self) -> None:
        """Open the planner's agent unless it's open, carrying on the saved conversation if
        there is one."""
        if self._agent is not None:
            return
        self._agent = self._desk.open(self, PLANNER, PLAN_TASK)
        if self._saved is not None:
            self._desk.agents.launcher.set_conversation(self._agent.id, self._saved)
        # The frontend learns from this which agent's output is the planner's.
        await self._desk.publish(
            AgentStarted(agent_id=self._agent.id, role=PLANNER, task_id=PLAN_TASK)
        )

    async def _start_fresh(
        self, turn_id: str, prompt: str, settings: Settings
    ) -> AsyncIterator[Event]:
        """Turn ``turn_id`` again, in a fresh conversation, after its TurnStarted is out."""
        assert self._agent is not None  # It was opened to try resuming.
        self._desk.agents.launcher.set_conversation(self._agent.id, None)
        self._told = None  # A fresh conversation hasn't heard the steps.
        yield Notice(message=RESUME_FAILED_NOTE)
        async for event in self._converse(turn_id, prompt, settings):
            if not isinstance(event, TurnStarted):
                yield event

    async def _converse(
        self, turn_id: str, prompt: str, settings: Settings
    ) -> AsyncIterator[Event]:
        """Turn ``turn_id`` of the planner's conversation, on ``prompt``."""
        assert self._agent is not None
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
        await self._tasks.publish()
        return Reply(f"Added draft task {task_id}; the user will accept or drop it.")

    async def _report_progress(self, arguments: dict[str, Any]) -> Reply:
        assert self._agent is not None  # It called, so it was opened.
        return await report_progress(self._desk.publish, self._agent.id, PLAN_TASK, arguments)
