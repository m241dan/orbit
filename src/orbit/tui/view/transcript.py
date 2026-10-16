"""Transcript entries: what each piece of a conversation reads as."""

from typing import assert_never

from orbit.protocol import (
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    Notice,
    ProgressNoted,
    SkipReason,
    StartupChecked,
    StepState,
    TaskGaveUp,
    TaskMerged,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
)
from orbit.tui.view.common import EntryKind
from orbit.tui.view.tool_calls import orbit_request_text, tool_call_text

SKIP_REASONS: dict[SkipReason, str] = {
    "tools_off": "tools were off",
    "no_changes": "no files changed",
    "unchanged": "unchanged since the last failed check",
    "no_steps": "no verification steps configured",
}


COLLAPSED_KINDS: frozenset[EntryKind] = frozenset({"tool", "request"})
"""Kinds whose entries start collapsed: tool output and the arguments of agents' requests to
orbit are long and rarely read."""


def starts_collapsed(kind: EntryKind) -> bool:
    return kind in COLLAPSED_KINDS


def collapsible(text: str) -> bool:
    """Whether an entry has lines after its first to collapse away."""
    return "\n" in text


def entry_label(text: str, collapsed: bool) -> str:
    """``text`` as its entry shows it. A collapsible entry starts with a marker; ``collapsed``,
    it shows only its first line and how many more there are."""
    if not collapsible(text):
        return text
    first, _, rest = text.partition("\n")
    if not collapsed:
        return f"▾ {text}"
    more = rest.count("\n") + 1
    return f"▸ {first} (+{more} line{'s' if more != 1 else ''})"


GUTTER_BAR = "│"
"""The dim bar in front of each line of an entry the cursor isn't on."""
GUTTER_MARKER = "▌"
"""The marker that replaces the bar on every line of the cursor's entry."""


def gutter(marked: bool) -> str:
    """The 2 columns in front of every row of an entry: the marker when it's ``marked`` as the
    cursor's, else a bar."""
    return f"{GUTTER_MARKER if marked else GUTTER_BAR} "


def output_entry(
    event: AssistantMessage | ToolCallStarted | ToolCallFinished,
) -> tuple[str, EntryKind]:
    """A piece of a turn's output as transcript text and the kind of entry to show it as.

    An agent's output is labelled with the agent, to tell it from the conversation's.
    """
    return agent_labelled(_output(event), event.agent_id)


def _output(
    event: AssistantMessage | ToolCallStarted | ToolCallFinished,
) -> tuple[str, EntryKind]:
    match event:
        case AssistantMessage(text=text):
            return text, "assistant"
        case ToolCallStarted(name=name, arguments=arguments):
            return tool_call_text(name, arguments), "tool"
        case ToolCallFinished(output=output, is_error=True):
            return output, "error"
        case ToolCallFinished(output=output):
            return output, "tool"
        case _:
            assert_never(event)


EXIT_ENTRIES: dict[AgentExitReason, tuple[str, EntryKind]] = {
    "finished": ("exited", "agent"),
    "killed": ("stopped", "agent"),
    "crashed": ("crashed", "error"),
}


def agent_entry(event: AgentStarted | TaskGaveUp | AgentExited) -> tuple[str, EntryKind]:
    """An agent starting, giving up or ending, as a transcript entry."""
    match event:
        case AgentStarted(agent_id=agent, role=role):
            return f"▶ {agent} ({role}) started", "agent"
        case TaskGaveUp(agent_id=agent, reason=reason):
            return f"🏳 {agent} gave up: {reason}", "error"
        case AgentExited(agent_id=agent, reason=reason):
            text, kind = EXIT_ENTRIES[reason]
            return f"■ {agent} {text}", kind
        case _:
            assert_never(event)


def exchange_entry(event: AgentRequest | AgentReply | ProgressNoted) -> tuple[str, EntryKind]:
    """A message between an agent and orbit, as a transcript entry."""
    match event:
        case AgentRequest(agent_id=agent, tool=tool, arguments=arguments):
            return f"{agent} → orbit: {orbit_request_text(tool, arguments)}", "request"
        case AgentReply(agent_id=agent, text=text, is_error=is_error):
            return f"orbit → {agent}: {text}", "error" if is_error else "agent"
        case ProgressNoted(agent_id=agent, note=note):
            return f"{agent}: {note}", "agent"
        case _:
            assert_never(event)


# Lines of a failed startup check's output the conversation quotes.
STARTUP_TAIL_LINES = 20


def startup_entry(event: StartupChecked) -> tuple[str, EntryKind]:
    """How the startup check went: passed, or which step failed, the end of its output, and the
    draft task added to fix it."""
    if event.passed:
        return "✅ Startup check passed", "verify"
    tail = "\n".join(event.output.splitlines()[-STARTUP_TAIL_LINES:])
    return (
        f"❌ Startup check failed: {event.step}\n\n{tail}\n\n"
        f"📝 Added a draft task: Fix failing check: {event.step}. If the steps themselves are"
        " wrong, press ctrl+e to reconfigure them.",
        "error",
    )


def note_entry(event: TaskMerged | Notice | StartupChecked) -> tuple[str, EntryKind]:
    """What came of merging a task's work, a notice, or the startup check, as a transcript
    entry."""
    match event:
        case TaskMerged(result="merged"):
            return "⇢ Merged into the base branch.", "verify"
        case TaskMerged(result=result, detail=detail):
            return f"⚠ Not merged ({result}): {detail}", "verify"
        case Notice(message=message):
            return f"📌 {message}", "verify"
        case StartupChecked():
            return startup_entry(event)
        case _:
            assert_never(event)


AgentNote = (
    AgentStarted
    | TaskGaveUp
    | AgentExited
    | AgentRequest
    | AgentReply
    | ProgressNoted
    | Notice
    | StartupChecked
)
"""Events shown as a single transcript entry each: agents' activity, notices and the startup
check's result."""


def activity_entry(event: AgentNote) -> tuple[str, EntryKind]:
    if isinstance(event, AgentStarted | TaskGaveUp | AgentExited):
        return agent_entry(event)
    if isinstance(event, AgentRequest | AgentReply | ProgressNoted):
        return exchange_entry(event)
    return note_entry(event)


def turn_end_entry(reason: TurnEndReason) -> tuple[str, EntryKind] | None:
    """A note on how a turn ended, unless it ended as expected or its error was shown."""
    if reason in ("completed", "error"):
        return None
    return f"[turn ended: {reason}]", "tool"


def agent_labelled(entry: tuple[str, EntryKind], agent: str | None) -> tuple[str, EntryKind]:
    """``entry`` with its text prefixed by ``agent``'s id, so agents sharing a tab can be told
    apart; unchanged when there's no agent."""
    text, kind = entry
    return entry if agent is None else (f"{agent}: {text}", kind)


def task_started_entry(number: str, state: TaskState | None) -> tuple[str, EntryKind]:
    """A task's header: its ``number`` and prompt; a task that isn't on the list (``state``
    None) is headed by ``number`` alone, which is then its id."""
    if state is None:
        return f"Task {number}", "user"
    return f"Task {number}: {state.task.prompt}", "user"


def verification_started_entry(step: StepState) -> tuple[str, EntryKind]:
    return f"🔍 Verifying: {step.name}…", "verify"


def verification_skipped_entry(reason: SkipReason) -> tuple[str, EntryKind]:
    return f"⏩ Verification skipped: {SKIP_REASONS[reason]}", "verify"


def verification_finished_entry(step: StepState, passed: bool) -> tuple[str, EntryKind]:
    if passed:
        return f"✅ Verification passed: {step.name}", "verify"
    return f"❌ Verification failed: {step.name}", "error"
