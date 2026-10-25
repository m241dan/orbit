"""What a span records of what its work cost, in OpenTelemetry's GenAI terms where they fit."""

from orbit.protocol import Usage
from orbit.tracing import AttributeValue


def usage_attributes(usage: Usage) -> dict[str, AttributeValue]:
    """The attributes recording ``usage``: its tokens of each kind, its cost in dollars and, if
    it spent any, its credits."""
    credits: dict[str, AttributeValue] = {"orbit.cost.credits": usage.credits}
    return {
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
        "gen_ai.usage.cache_creation.input_tokens": usage.cache_creation_input_tokens,
        "gen_ai.usage.cache_read.input_tokens": usage.cache_read_input_tokens,
        "orbit.cost.usd": usage.cost_usd,
        **(credits if usage.credits > 0 else {}),
    }
