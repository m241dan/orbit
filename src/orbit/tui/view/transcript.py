"""Transcript entries: what each piece of a conversation reads as."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from orbit.protocol import (
    PLANNER,
    AgentExited,
    AgentExitReason,
    AgentReply,
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
    TurnEndReason,
    TurnFinished,
    Usage,
)
from orbit.tui.view.common import EntryKind, counted, spinner_frame, text_lines, token_text
from orbit.tui.view.tool_calls import ToolCall, orbit_request_line, tool_call_line

SKIP_REASONS: dict[SkipReason, str] = {
    "tools_off": "tools were off",
    "no_changes": "no files changed",
    "unchanged": "unchanged since the last failed check",
    "no_steps": "no verification steps configured",
}


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


def conversation_title(label: str | None, thinking_hidden: bool) -> str:
    """The conversation window's title: the tab's ``label`` after it unless All is shown
    (None), and a note when ``thinking_hidden``."""
    parts = [CONVERSATION_TITLE, *(() if label is None else (label,))]
    if thinking_hidden:
        parts.append("thinking hidden")
    return " · ".join(parts)


PLANNER_BUSY = "Planner is busy"
"""The note when the user sends a prompt while the planner is still answering."""


def prompt_title(busy: bool) -> str:
    """The prompt box's title, naming the planner it talks to and whether it's ``busy``."""
    return " · ".join(["Prompt", PLANNER, *(("busy",) if busy else ())])


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


AgentNote = AgentStarted | TaskGaveUp | AgentReply | ProgressNoted | Notice | StartupChecked
"""Events shown as a single transcript entry each: agents' activity, notices and the startup
check's result."""


def activity_entry(event: AgentNote) -> tuple[str, EntryKind]:
    if isinstance(event, AgentStarted | TaskGaveUp):
        return agent_entry(event)
    if isinstance(event, AgentReply | ProgressNoted):
        return exchange_entry(event)
    return note_entry(event)


RULE_KINDS: frozenset[EntryKind] = frozenset({"rule", "error_rule"})
"""Kinds whose entries are drawn as a horizontal rule across their width, their text set in
it: the rule ending every turn, and an agent's run."""


def ruled(kind: EntryKind) -> bool:
    return kind in RULE_KINDS


TURN_END_LABELS: dict[TurnEndReason, str] = {
    "completed": "done",
    "cancelled": "stopped",
    "max_steps": "out of steps",
    "error": "error",
}


def duration_text(seconds: float) -> str:
    """``seconds`` as a turn's time reads: ``42s``, ``1m12s``, or ``1h03m`` from an hour."""
    whole = int(seconds)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def usage_text(usage: Usage) -> str:
    """The tokens a turn read (``↑``, its input with what was cached and read from the cache)
    and wrote (``↓``)."""
    return f"↑{token_text(usage.read_tokens)} ↓{token_text(usage.output_tokens)}"


def _turn_cost(usage: Usage | None, elapsed: float | None) -> list[str]:
    """How long a turn took, the ``usage``'s time else ``elapsed`` seconds if known, and its
    tokens when it has ``usage``."""
    if usage is None:
        return [] if elapsed is None else [duration_text(elapsed)]
    return [duration_text(usage.duration_ms / 1000), usage_text(usage)]


def turn_rule_entry(
    event: TurnFinished, elapsed: float | None, speaker: str | None
) -> tuple[str, EntryKind]:
    """The rule ending ``event``'s turn: how it ended, headed by its ``speaker`` if any, then
    how long it took (the usage's time, else ``elapsed`` seconds, if known) and its tokens when
    it has usage. A turn ending in an error is a rule of its own kind."""
    label = TURN_END_LABELS[event.reason]
    return _rule(label, speaker, _turn_cost(event.usage, elapsed), event.reason == "error")


def _rule(label: str, speaker: str | None, cost: list[str], error: bool) -> tuple[str, EntryKind]:
    """A rule reading ``label``, headed by its ``speaker`` if any, then its ``cost``; an
    ``error`` rule is of its own kind."""
    parts = [label if speaker is None else f"{speaker} {label}", *cost]
    return " · ".join(parts), "error_rule" if error else "rule"


EXIT_LABELS: dict[AgentExitReason, str] = {
    "finished": "done",
    "killed": "stopped",
    "crashed": "crashed",
}


@dataclass(frozen=True)
class DrawnRule:
    """The rule drawn for an agent's last turn: the ``turn``'s end, ``elapsed`` seconds after
    it started, if known."""

    turn: TurnFinished
    elapsed: float | None


@dataclass(frozen=True)
class ExitRule:
    """The rule an agent's exit draws: its ``text`` and ``kind``, either replacing the rule of
    the agent's last turn (``replaces``) or added after it."""

    text: str
    kind: EntryKind
    replaces: bool


def exit_rule(
    event: AgentExited, drawn: DrawnRule | None, elapsed: float | None, speaker: str | None
) -> ExitRule | None:
    """What ``event``'s agent exiting does to the rules ending its run, so the run ends in
    one: with a rule ``drawn`` for its last turn, a ``done`` one turns ``stopped`` or
    ``crashed`` when the agent was killed or crashed, and is left as it is otherwise (None).
    With none, a rule of its own says how the agent exited, headed by its ``speaker`` if any,
    ``elapsed`` seconds after its last turn (or the agent) started, if known."""
    label = EXIT_LABELS[event.reason]
    crashed = event.reason == "crashed"
    if drawn is None:
        cost = [] if elapsed is None else [duration_text(elapsed)]
        return ExitRule(*_rule(label, speaker, cost, crashed), replaces=False)
    if event.reason == "finished" or drawn.turn.reason != "completed":
        return None
    cost = _turn_cost(drawn.turn.usage, drawn.elapsed)
    return ExitRule(*_rule(label, speaker, cost, crashed), replaces=True)


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
