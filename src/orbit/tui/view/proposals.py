"""Proposed checks and the user's decisions on them."""

import shlex

from orbit.protocol import ChecksProposed, DecideProposal, ProposalAction, ProposalDecided
from orbit.tui.view.common import EntryKind

PROPOSAL_KEYS_HINT = "a accept · r revise · x end"
FEEDBACK_HINT = "enter send · esc back"
FEEDBACK_PLACEHOLDER = "What should change?"

DECISION_NOTES: dict[ProposalAction, str] = {
    "accept": "proposal accepted",
    "revise": "changes asked for: ",
    "end": "task ended",
}


def proposal_title(agent_id: str) -> str:
    """The proposal modal's border title, naming the agent ``agent_id`` that proposed."""
    return f"Proposed checks · {agent_id}"


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


def revision(proposal: ChecksProposed, feedback: str) -> DecideProposal | None:
    """The decision asking for the changes ``feedback`` says to ``proposal``; none while it's
    blank."""
    text = feedback.strip()
    if not text:
        return None
    return DecideProposal(task_id=proposal.task_id, action="revise", feedback=text)


def decision_entry(agent: str, decision: DecideProposal | ProposalDecided) -> tuple[str, EntryKind]:
    """The user's ``decision`` on ``agent``'s proposal, as a transcript entry."""
    note = DECISION_NOTES[decision.action]
    if decision.action == "revise":
        note += decision.feedback
    return f"orbit → {agent}: {note}", "agent"
