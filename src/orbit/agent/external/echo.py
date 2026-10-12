"""A stand-in model client, for running the in-house loop without a real model."""

from collections.abc import Sequence

from orbit.agent.domain.conversation import Message, ModelResponse, ToolSpec


class EchoModel:
    """Placeholder model that repeats the last user message. No tool use."""

    async def complete(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return ModelResponse(text=f"echo: {last_user}")
