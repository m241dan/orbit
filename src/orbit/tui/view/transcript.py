"""Transcript entries: what each piece of a conversation reads as."""

from collections.abc import Iterable, Mapping
from typing import assert_never

from orbit.protocol import (
    PLANNER,
    AgentEntry,
    AgentReply,
    AgentStarted,
    AssistantMessage,
    CommandRefused,
    ConsultationAnswered,
    ErrorOccurred,
    LogGap,
    Notice,
    ProgressNoted,
    StartupChecked,
    TaskGaveUp,
    TaskMerged,
    ToolCallFinished,
)
from orbit.tui.view.common import EntryKind, counted, ordinal, spinner_frame, text_lines
from orbit.tui.view.tool_calls import ToolCall, orbit_request_line, tool_call_line

COLLAPSED_KINDS: frozenset[EntryKind] = frozenset({"tool"})
"""Kinds whose entries start collapsed: tool output is long and rarely read."""

PROMPT_LINES_SHOWN = 15
"""The most lines a prompt of the user's starts expanded with."""


def starts_collapsed(kind: EntryKind, text: str) -> bool:
    """Whether an entry of ``kind`` showing ``text`` starts collapsed: one of
    :data:`COLLAPSED_KINDS` with lines to collapse, or a prompt of the user's longer than
    :data:`PROMPT_LINES_SHOWN`."""
    if kind == "prompt":
        return text.count("\n") >= PROMPT_LINES_SHOWN
    return kind in COLLAPSED_KINDS and collapsible(text)


def collapsible(text: str) -> bool:
    """Whether an entry has lines after its first to collapse away."""
    return "\n" in text


EXPANDED_MARKER = "▾ "
"""The marker in front of an expanded entry that can be collapsed."""
PROMPT_MARKER = "❯ "
"""The marker in front of the user's prompts, ahead of any collapse marker."""


def entry_prefix(kind: EntryKind) -> str:
    """What goes in front of an entry of ``kind`` before anything else: the prompt marker for
    the user's prompts, nothing for the rest."""
    return PROMPT_MARKER if kind == "prompt" else ""


def entry_label(kind: EntryKind, text: str, collapsed: bool) -> str:
    """``text`` as an entry of ``kind`` shows it, after its prefix. A collapsible entry starts
    with a marker; ``collapsed``, it shows only its first line and how many more there are."""
    return entry_prefix(kind) + _collapse_label(text, collapsed)


def _collapse_label(text: str, collapsed: bool) -> str:
    if not collapsible(text):
        return text
    first, _, rest = text.partition("\n")
    if not collapsed:
        return f"{EXPANDED_MARKER}{text}"
    more = rest.count("\n") + 1
    return f"▸ {first} (+{counted(more, 'line')})"


MARKDOWN_KINDS: frozenset[EntryKind] = frozenset({"assistant", "prompt"})
"""Kinds whose entries show their text as markdown: agents' and the planner's messages, and
the user's prompts."""


def shows_markdown(kind: EntryKind, collapsed: bool) -> bool:
    """Whether an entry of ``kind`` shows its text as markdown: one ``collapsed`` to its first
    line shows that line as it is."""
    return kind in MARKDOWN_KINDS and not collapsed


BANDED_KINDS: frozenset[EntryKind] = frozenset({"prompt"})
"""Kinds whose entries are drawn on a band of their style's background across their width:
the user's prompts, so they stand out."""


def banded(kind: EntryKind) -> bool:
    return kind in BANDED_KINDS


def hanging_indent(kind: EntryKind, text: str) -> tuple[str, str]:
    """What goes in front of the first row of an entry's markdown and in front of every row
    after it: the prefix of an entry of ``kind`` and the marker of a collapsible entry, hanging
    beside it; nothing beside an assistant's message of a line."""
    first = entry_prefix(kind) + (EXPANDED_MARKER if collapsible(text) else "")
    return first, " " * len(first)


def speaker(agent: str | None, roles: Mapping[str, str]) -> str | None:
    """Who ``agent``'s entries are spoken by: the agent, or None for the planner's, which are
    those of an agent ``roles`` has pinned in the planner role, the user's prompts to it
    (agent ``PLANNER``) and those with no agent, all one speaker of their own."""
    return None if agent is None or roles.get(agent, agent) == PLANNER else agent


CONVERSATION_TITLE = "Conversation"
"""The conversation window's title, before its tab's label and filter."""


SESSION_NAME_WIDTH = 30
"""The most characters of the open session's name the conversation window's title shows."""


def conversation_title(
    label: str | None, thinking_hidden: bool, *, session: str | None = None
) -> str:
    """The conversation window's title: the tab's ``label`` after it unless All is shown
    (None), a note when ``thinking_hidden``, and the open ``session``'s name, if it has one,
    at the end."""
    parts = [CONVERSATION_TITLE, *(() if label is None else (label,))]
    if thinking_hidden:
        parts.append("thinking hidden")
    title = " · ".join(parts)
    return f"{title} — {_session_name(session)}" if session else title


def _session_name(name: str) -> str:
    if len(name) <= SESSION_NAME_WIDTH:
        return name
    return name[: SESSION_NAME_WIDTH - 1] + "…"


def busy_note(label: str, answering: str | None = None) -> str:
    """The note when the user sends a prompt to the conversational role labelled ``label``
    while its lane is busy, naming the role labelled ``answering``, the one answering there, or
    else that role, when it isn't known: ``Planner is busy``."""
    return f"{answering or label} is busy"


def prompt_placeholder(label: str) -> str:
    """What the empty prompt says while it talks to the conversational role labelled
    ``label``."""
    return f"Press i to talk to {label}, esc to stop typing"


READ_ONLY_MARK = "read-only"
"""The prompt box's mark while another orbit owns the repository."""


def prompt_title(busy: bool, read_only: bool = False, role: str = PLANNER) -> str:
    """The prompt box's title, naming the conversational ``role`` it talks to, whether that's
    ``busy`` and whether the orbit is ``read_only``."""
    marks = (*(("busy",) if busy else ()), *((READ_ONLY_MARK,) if read_only else ()))
    return " · ".join(["Prompt", role, *marks])


def speaker_label(kind: EntryKind, current: str | None, previous: str | None) -> str | None:
    """The label heading an entry of ``kind`` spoken by ``current`` (see :func:`speaker`): the
    speaker, for an agent's message when the ``previous`` entry in its log was spoken by
    someone else; None otherwise."""
    if kind != "assistant" or current is None or current == previous:
        return None
    return current


GUTTER_BAR = "│"
"""The dim bar in front of each line of an entry the cursor isn't on."""
GUTTER_MARKER = "▌"
"""The marker that replaces the bar on every line of the cursor's entry."""


def gutter(marked: bool) -> str:
    """The 2 columns in front of every row of an entry: the marker when it's ``marked`` as the
    cursor's, else a bar."""
    return f"{GUTTER_MARKER if marked else GUTTER_BAR} "


def output_entry(event: AssistantMessage | ToolCallFinished) -> tuple[str, EntryKind]:
    """A piece of a turn's output as transcript text and the kind of entry to show it as: a
    message, or the output of a tool call whose start wasn't shown.

    An agent's tool output and errors are labelled with the agent, to tell them from the
    conversation's; its messages are headed by a label row instead (see :func:`speaker_label`).
    """
    entry = _output(event)
    return entry if entry[1] == "assistant" else agent_labelled(entry, event.agent_id)


def tool_call_entry(call: ToolCall, agent: str | None, now: float) -> tuple[str, EntryKind]:
    """``call`` by ``agent`` as its one-line entry, its spinner as it is at ``now`` seconds
    while it runs: a tool call labelled with the agent like the rest of its output, a request
    to orbit naming the agent as it asks."""
    if call.request:
        return orbit_request_line(call, agent, spinner_frame(now))
    return agent_labelled(tool_call_line(call, spinner_frame(now)), agent)


def _output(event: AssistantMessage | ToolCallFinished) -> tuple[str, EntryKind]:
    match event:
        case AssistantMessage(text=text):
            return text, "assistant"
        case ToolCallFinished(output=output, is_error=True):
            return output, "error"
        case ToolCallFinished(output=output):
            return output, "tool"
        case _:
            assert_never(event)


def agent_entry(event: AgentStarted | TaskGaveUp) -> tuple[str, EntryKind]:
    """An agent starting or giving up, as a transcript entry."""
    match event:
        case AgentStarted(agent_id=agent, role=role):
            return f"▶ {agent} ({role}) started", "agent"
        case TaskGaveUp(agent_id=agent, reason=reason):
            return f"🏳 {agent} gave up: {reason}", "error"
        case _:
            assert_never(event)


def exchange_entry(event: AgentReply | ProgressNoted) -> tuple[str, EntryKind]:
    """A message between an agent and orbit, as a transcript entry: a reply to no request
    shown, or a progress note."""
    match event:
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
    tail = "\n".join(text_lines(event.output)[-STARTUP_TAIL_LINES:])
    return (
        f"❌ Startup check failed: {event.step}\n\n{tail}\n\n"
        f"📝 Added a draft task: Fix failing check: {event.step}. If the steps themselves are"
        " wrong, press ctrl+e to reconfigure them.",
        "error",
    )


def gap_entry(event: LogGap) -> tuple[str, EntryKind]:
    """The events lost when storage failed, as a transcript entry."""
    return f"⚠ Events {event.first_seq}–{event.last_seq} weren't saved", "error"


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


_TELEMETRY_LOGGERS = ("opentelemetry", "orbit.telemetry")
"""The loggers, with those under them, whose warnings are telemetry's."""


def warning_entry(logger: str, message: str) -> tuple[str, EntryKind]:
    """A warning logged as ``message`` by ``logger`` as a transcript entry, naming telemetry
    when it's telemetry's."""
    if any(logger == name or logger.startswith(f"{name}.") for name in _TELEMETRY_LOGGERS):
        return f"⚠ Telemetry: {message}", "verify"
    return f"⚠ {message}", "verify"


AgentNote = (
    AgentStarted | TaskGaveUp | AgentReply | ProgressNoted | Notice | StartupChecked | LogGap
)
"""Events shown as a single transcript entry each: agents' activity, notices, the startup
check's result and events lost when storage failed."""


def activity_entry(event: AgentNote) -> tuple[str, EntryKind]:
    if isinstance(event, AgentStarted | TaskGaveUp):
        return agent_entry(event)
    if isinstance(event, AgentReply | ProgressNoted):
        return exchange_entry(event)
    if isinstance(event, LogGap):
        return gap_entry(event)
    return note_entry(event)


RULE_KINDS: frozenset[EntryKind] = frozenset({"rule", "error_rule"})
"""Kinds whose entries are drawn as a horizontal rule across their width, their text set in
it: the rule ending every turn, and an agent's run."""


def ruled(kind: EntryKind) -> bool:
    return kind in RULE_KINDS


def error_entry(event: ErrorOccurred | CommandRefused) -> tuple[str, EntryKind]:
    """``event``'s error message, labelled with its agent."""
    return agent_labelled((event.message, "error"), error_agent(event))


def error_agent(event: ErrorOccurred | CommandRefused) -> str | None:
    """The agent whose error ``event`` is, if any; a refused command is no agent's."""
    return event.agent_id if isinstance(event, ErrorOccurred) else None


def agent_labelled(entry: tuple[str, EntryKind], agent: str | None) -> tuple[str, EntryKind]:
    """``entry`` with its text prefixed by ``agent``'s id, so agents sharing a tab can be told
    apart; unchanged when there's no agent."""
    text, kind = entry
    return entry if agent is None else (f"{agent}: {text}", kind)


REPLY_LINES_SHOWN = 3
"""The most lines of a consultant's reply its entry shows; the detail pane shows them all."""


def asked_entry(consultant: str, title: str, position: int) -> tuple[str, EntryKind]:
    """A consultation asked of the role labelled ``consultant``, titled ``title``, as its
    asker's entry: with its place in line while it's ``position`` 2 or later."""
    waiting = f" ({ordinal(position)} in line)" if position > 1 else ""
    return f"→ Asked {consultant}: {title}{waiting}", "agent"


def answered_entry(event: ConsultationAnswered, consultant: str) -> tuple[str, EntryKind]:
    """``event``'s answer from the role labelled ``consultant`` as its asker's entry: a header
    counting the reply's lines, then its first :data:`REPLY_LINES_SHOWN`; or, interrupted, a
    line saying why."""
    who = consultant[:1].upper() + consultant[1:]
    if event.interrupted is not None:
        why = event.interrupted
        return f"↩ {who}'s consultation '{event.title}' was interrupted: {why}", "agent"
    lines = text_lines(event.reply)
    header = f"↩ {who}'s reply to '{event.title}' · {counted(len(lines), 'line')}"
    return "\n".join([header, *lines[:REPLY_LINES_SHOWN]]), "agent"


def reply_call(event: ConsultationAnswered, header: str) -> ToolCall:
    """``event``'s reply as the detail pane shows it, summarized by its entry's ``header``."""
    call_id = f"consultation-{event.consultation}"
    return ToolCall(call_id, "consultation", header, {}, status="passed", output=event.reply)


def role_label(role: str, agents: Iterable[AgentEntry]) -> str:
    """The label of ``role`` among the ``agents`` listed, or its name if it isn't listed."""
    return next((agent.label for agent in agents if agent.name == role), role)
