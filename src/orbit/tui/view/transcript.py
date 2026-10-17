"""Transcript entries: what each piece of a conversation reads as."""

from collections.abc import Sequence
from typing import ClassVar, Literal, assert_never

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Heading, Markdown, MarkdownContext, MarkdownElement
from rich.style import Style
from rich.text import Text

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ErrorOccurred,
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
    TurnFinished,
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
    return f"▸ {first} (+{more} line{'s' if more != 1 else ''})"


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


class LeftHeading(Heading):
    """A heading drawn bold and left-aligned, h1 also underlined, rather than centred."""

    def on_enter(self, context: MarkdownContext) -> None:
        self.text = Text()
        context.enter_style(Style(bold=True, underline=self.tag == "h1"))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        yield text


class EntryMarkdown(Markdown):
    """Markdown as an entry shows it: headings drawn as :class:`LeftHeading`."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": LeftHeading,
    }


def entry_markdown(text: str, style: Style) -> Markdown:
    """``text`` as markdown in the entry's ``style``, its links shown as ``text (url)``."""
    return EntryMarkdown(text, style=style, hyperlinks=False)


def speaker(agent: str | None) -> str | None:
    """Who ``agent``'s entries are spoken by: the agent, or None for the planner's and those
    with no agent, which are one speaker of their own."""
    return None if agent == PLANNER else agent


def speaker_label(kind: EntryKind, agent: str | None, previous: str | None) -> str | None:
    """The label heading an entry of ``kind`` by ``agent``: the agent's id, for an agent's
    message when the ``previous`` entry in its log was by someone else; None otherwise."""
    current = speaker(agent)
    if kind != "assistant" or current is None or current == speaker(previous):
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


def output_entry(
    event: AssistantMessage | ToolCallStarted | ToolCallFinished,
) -> tuple[str, EntryKind]:
    """A piece of a turn's output as transcript text and the kind of entry to show it as.

    An agent's tool calls and errors are labelled with the agent, to tell them from the
    conversation's; its messages are headed by a label row instead (see :func:`speaker_label`).
    """
    entry = _output(event)
    return entry if entry[1] == "assistant" else agent_labelled(entry, event.agent_id)


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


TurnStep = Literal["message", "tool"]
"""What a turn's output was, in order: a message, or the start of a tool call."""


def final_answer(steps: Sequence[TurnStep]) -> tuple[int, ...]:
    """Which of a turn's messages are its final answer, by their place among the turn's
    messages: those after its last tool call, given the turn's ``steps``. A turn that ends on a
    tool call has none."""
    last_tool = max((index for index, step in enumerate(steps) if step == "tool"), default=-1)
    trailing = len(steps) - last_tool - 1
    total = steps.count("message")
    return tuple(range(total - trailing, total))


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


def turn_finished_entry(event: TurnFinished) -> tuple[str, EntryKind] | None:
    """A note on how ``event``'s turn ended, labelled with its agent; None when the turn ended
    as expected or its error was shown."""
    entry = turn_end_entry(event.reason)
    return None if entry is None else agent_labelled(entry, event.agent_id)


def error_entry(event: ErrorOccurred) -> tuple[str, EntryKind]:
    """``event``'s error message, labelled with its agent."""
    return agent_labelled((event.message, "error"), event.agent_id)


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
