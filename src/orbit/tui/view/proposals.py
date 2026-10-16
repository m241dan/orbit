"""Proposed checks and the user's decisions on them."""

import shlex

from orbit.protocol import ChecksProposed, DecideProposal, ProposalAction
from orbit.tui.view.common import EntryKind

DECISION_NOTES: dict[ProposalAction, str] = {
    "accept": "proposal accepted",
    "revise": "changes asked for: ",
    "end": "task ended",
}


def proposal_text(event: ChecksProposed) -> str:
    """The proposed setup, then each step in order, and that they all passed."""
    setup = event.setup
    lines = [f"Setup: {'none' if setup is None else shlex.join(setup.command)}"]
    if setup is not None and setup.watch:
        lines.append(f"  reruns when these change: {', '.join(setup.watch)}")
    lines += ["", "Steps:"]
    lines += [f"  {i}. {s.name}: {shlex.join(s.command)}" for i, s in enumerate(event.steps, 1)]
    lines += ["", "✅ All passed on the current code."]
    return "\n".join(lines)


def decision_entry(agent: str, decision: DecideProposal) -> tuple[str, EntryKind]:
    """The user's ``decision`` on ``agent``'s proposal, as a transcript entry."""
    note = DECISION_NOTES[decision.action]
    if decision.action == "revise":
        note += decision.feedback
    return f"orbit → {agent}: {note}", "agent"
