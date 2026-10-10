"""The agentic loop: call the model, run requested tools, repeat."""

from collections.abc import AsyncIterator

from orbit.agent.model import Message, ModelClient
from orbit.agent.tools import ToolRegistry
from orbit.protocol import (
    AssistantMessage,
    Event,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)

DEFAULT_MAX_STEPS = 20


async def run_turn(
    turn_id: str,
    history: list[Message],
    model: ModelClient,
    tools: ToolRegistry,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> AsyncIterator[Event]:
    """Run one turn against ``history``, appending to it as the turn progresses.

    The last entry in ``history`` should be the user's message.
    """
    yield TurnStarted(turn_id=turn_id)
    for _ in range(max_steps):
        response = await model.complete(history, tools.specs)
        history.append(
            Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
        )
        if response.text:
            yield AssistantMessage(turn_id=turn_id, text=response.text)
        if not response.tool_calls:
            yield TurnFinished(turn_id=turn_id, reason="completed")
            return

        for call in response.tool_calls:
            yield ToolCallStarted(
                turn_id=turn_id, call_id=call.id, name=call.name, arguments=call.arguments
            )
            try:
                output, is_error = await tools.run(call.name, call.arguments), False
            except Exception as exc:
                output, is_error = f"{type(exc).__name__}: {exc}", True
            history.append(Message(role="tool", content=output, tool_call_id=call.id))
            yield ToolCallFinished(
                turn_id=turn_id, call_id=call.id, output=output, is_error=is_error
            )

    yield TurnFinished(turn_id=turn_id, reason="max_steps")


class ModelLoopRunner:
    """Turn runner that drives :func:`run_turn` against a model client."""

    def __init__(
        self,
        model: ModelClient,
        tools: ToolRegistry | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self._model = model
        self._tools = tools or ToolRegistry()
        self._max_steps = max_steps
        self.history: list[Message] = []

    async def run_turn(self, turn_id: str, prompt: str) -> AsyncIterator[Event]:
        checkpoint = len(self.history)
        self.history.append(Message(role="user", content=prompt))
        try:
            async for event in run_turn(
                turn_id, self.history, self._model, self._tools, self._max_steps
            ):
                yield event
        except BaseException:
            # Drop the partial turn so history never holds unanswered tool calls.
            del self.history[checkpoint:]
            raise
