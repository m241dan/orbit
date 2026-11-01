"""How what's spent reads: what a usage cost, and its tokens and cost, shortly."""

from orbit.protocol import Usage
from orbit.tui.view.common import token_text

NO_COST = "$0.00"  # What a usage that cost nothing shows for its cost beside its tokens.


def cost_text(usage: Usage) -> str:
    """What ``usage`` cost, in what it has: dollars (``$1.20``), credits (``0.62 credits``) or
    both (``$1.20 · 0.62 credits``); empty when it has neither."""
    dollars = [f"${usage.cost_usd:.2f}"] if usage.cost_usd > 0 else []
    credits = [f"{usage.credits:.2f} credits"] if usage.credits > 0 else []
    return " · ".join(dollars + credits)


def usage_label(usage: Usage) -> str:
    """Tokens and cost, shortly: ``1.2M tok · $0.84``, or just ``0.62 credits`` for credits
    with no tokens, since a runtime that charges credits may not count tokens."""
    if usage.credits and not usage.tokens:
        return cost_text(usage)
    return f"{token_text(usage.tokens)} tok · {cost_text(usage) or NO_COST}"
