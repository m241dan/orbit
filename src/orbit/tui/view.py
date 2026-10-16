"""What the screen says: protocol state and events turned into text. Pure: no Textual.

The widgets decide where things go and when they redraw; this module decides what they read.
"""

import json
import shlex
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, TypeGuard, assert_never, get_args

from rich.cells import cell_len
from rich.text import Text

from orbit.protocol import (
    PLAN_TASK,
    STARTUP_TASK,
    AgentExited,
    AgentExitReason,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    ChecksProposed,
    DecideProposal,
    ErrorOccurred,
    Event,
    Notice,
    ProgressNoted,
    ProposalAction,
    ReviewStatus,
    Settings,
    SettingsChanged,
    SkipReason,
    StartupChecked,
    StepState,
    StepStatus,
    TaskGaveUp,
    TaskMerged,
    TasksChanged,
    TaskStarted,
    TaskState,
    TaskStatus,
    ToolAccess,
    ToolCallFinished,
    ToolCallStarted,
    TurnEndReason,
    TurnFinished,
    Usage,
    UsageChanged,
    UsageLimit,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
)

EntryKind = Literal["user", "assistant", "tool", "verify", "agent", "error"]
"""How a transcript entry is styled."""

TOOL_ACCESS_OPTIONS: tuple[ToolAccess, ...] = get_args(ToolAccess)
TOOL_ACCESS_LABELS: dict[ToolAccess, str] = {
    "off": "off",
    "restricted": "restricted",
    "full": "full (auto-approved)",
}
TOOL_ACCESS_DESCRIPTIONS: dict[ToolAccess, str] = {
    "off": "off: no tools",
    "restricted": "restricted: read-only; anything needing approval is refused",
    "full": "full: auto-approve everything",
}

SKIP_REASONS: dict[SkipReason, str] = {
    "tools_off": "tools were off",
    "no_changes": "no files changed",
    "unchanged": "unchanged since the last failed check",
    "no_steps": "no verification steps configured",
}

# The arc spinner beside work in progress, and how fast it turns.
SPINNER_FRAMES = "◜◠◝◞◡◟"
SPINNER_FPS = 8

# Emoji drawn two cells wide on their own (no variation selector), so rows line up.
# Running tasks and steps show the spinner instead.
STATUS_ICONS: dict[TaskStatus, str] = {
    "pending": "⏳",
    "verifying": "🔍",
    "completed": "✅",
    "unverified": "❗",
    "cancelled": "🛑",
    "error": "❌",
    "gave_up": "🏳",
    "draft": "📝",
}
STEP_ICONS: dict[StepStatus, str] = {
    "pending": "⏳",
    "passed": "✅",
    "failed": "❌",
    "cancelled": "🛑",
    "skipped": "⏩",
}


# Snapshot events a later one of the same type replaces, unless one of these events, which
# read the state the snapshot sets, lies between them.
SUPERSEDED_UNLESS: Mapping[type[Event], tuple[type[Event], ...]] = MappingProxyType(
    {
        TasksChanged: (
            TaskStarted,
            VerificationStarted,
            VerificationSkipped,
            VerificationFinished,
            TaskMerged,
        ),
        VerificationChanged: (VerificationStarted, VerificationFinished),
        ReviewStatus: (),
        SettingsChanged: (),
    }
)


def coalesce(events: Sequence[Event]) -> list[Event]:
    """The ``events`` to render, in order: without the snapshots a later one replaces before
    anything reads them."""
    kept: list[Event] = []
    superseded: set[type[Event]] = set()
    for event in reversed(events):
        kind = type(event)
        if kind not in superseded:
            kept.append(event)
        if kind in SUPERSEDED_UNLESS:
            superseded.add(kind)
        superseded -= {s for s in superseded if kind in SUPERSEDED_UNLESS[s]}
    kept.reverse()
    return kept


def spinner_frame(now: float) -> str:
    """The spinner frame at ``now`` seconds, padded to two columns like the emoji beside it."""
    return SPINNER_FRAMES[int(now * SPINNER_FPS) % len(SPINNER_FRAMES)] + " "


def model_label(settings: Settings) -> str:
    return settings.model or "default"


def model_options(choices: Sequence[str]) -> tuple[tuple[str | None, ...], list[str]]:
    """The models a picker offers, the default (None) first, and their labels."""
    return (None, *choices), ["default", *choices]


def tool_access_labels() -> list[str]:
    """The labels a picker shows for ``TOOL_ACCESS_OPTIONS``, in the same order."""
    return [TOOL_ACCESS_DESCRIPTIONS[a] for a in TOOL_ACCESS_OPTIONS]


def settings_summary(settings: Settings) -> str:
    """The settings as the prompt shows them.

    The model is the planner's, and the default for new tasks; the tool access is
    only the default for new tasks.
    """
    tools = TOOL_ACCESS_LABELS[settings.tool_access]
    return f"planner's model: {model_label(settings)} · new tasks' tools: {tools}"


# Transcript entries

COLLAPSED_KINDS: frozenset[EntryKind] = frozenset({"tool"})
"""Kinds whose entries start collapsed: tool output is long and rarely read."""


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
            return _tool_call(name, arguments), "tool"
        case ToolCallFinished(output=output, is_error=True):
            return output, "error"
        case ToolCallFinished(output=output):
            return output, "tool"
        case _:
            assert_never(event)


def _tool_call(name: str, arguments: Mapping[str, Any]) -> str:
    """A tool call as one line summing it up, with its full ``arguments`` on the lines after."""
    if not arguments:
        return f"→ {name}"
    full = json.dumps(arguments, indent=2, ensure_ascii=False)
    return f"→ {tool_call_summary(name, arguments)}\n{full}"


TOOL_CALL_SUMMARY_LIMIT = 120
"""The most characters a tool call's summary line shows."""
ORBIT_TOOL_PREFIX = "mcp__orbit__"
ORBIT_TOOL_FIELDS = ("summary", "title", "note", "reason", "name")
"""The arguments of orbit's own tools that sum up a call, best first."""

type ArgumentExtractor = Callable[[Mapping[str, Any]], str | None]


def _text(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    return value if isinstance(value, str) else None


def _field(key: str) -> ArgumentExtractor:
    return lambda arguments: _text(arguments, key)


def _first_line(arguments: Mapping[str, Any]) -> str | None:
    command = _text(arguments, "command")
    return None if command is None else command.partition("\n")[0]


def _searched(arguments: Mapping[str, Any]) -> str | None:
    pattern = _text(arguments, "pattern")
    path = _text(arguments, "path")
    return pattern if pattern is None or path is None else f"{pattern} in {path}"


def _todos(arguments: Mapping[str, Any]) -> str | None:
    todos = arguments.get("todos")
    return f"{len(todos)} todos" if isinstance(todos, list) else None


KEY_ARGUMENTS: Mapping[str, ArgumentExtractor] = MappingProxyType(
    {
        "Read": _field("file_path"),
        "Write": _field("file_path"),
        "Edit": _field("file_path"),
        "MultiEdit": _field("file_path"),
        "NotebookEdit": _field("notebook_path"),
        "Bash": _first_line,
        "Grep": _searched,
        "Glob": _searched,
        "WebSearch": _field("query"),
        "WebFetch": _field("url"),
        "Task": _field("description"),
        "Agent": _field("description"),
        "TodoWrite": _todos,
    }
)
"""How to find the argument that sums up a call to each of Claude Code's built-in tools."""


def tool_call_summary(name: str, arguments: Mapping[str, Any]) -> str:
    """The tool ``name`` and the argument that sums up its call, clipped to one short line.

    A tool with no such argument among ``arguments`` shows them all, as compact JSON.
    """
    if name.startswith(ORBIT_TOOL_PREFIX):
        summary = _orbit_tool_summary(name.removeprefix(ORBIT_TOOL_PREFIX), arguments)
    else:
        summary = _built_in_tool_summary(name, arguments)
    return _clip(summary, TOOL_CALL_SUMMARY_LIMIT)


def _orbit_tool_summary(tool: str, arguments: Mapping[str, Any]) -> str:
    texts = (_text(arguments, key) for key in ORBIT_TOOL_FIELDS)
    text = next((text for text in texts if text is not None), None)
    return tool if text is None else f"{tool} {_squeezed(text)}"


def _built_in_tool_summary(name: str, arguments: Mapping[str, Any]) -> str:
    extract = KEY_ARGUMENTS.get(name)
    key = None if extract is None else extract(arguments)
    if key is None:
        return f"{name} {json.dumps(arguments, ensure_ascii=False)}"
    return f"{name} {_squeezed(key)}"


def _squeezed(text: str) -> str:
    """``text`` with each run of whitespace, newlines included, as a single space."""
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


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
            return f"{agent} → orbit: {tool} {json.dumps(arguments)}", "agent"
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


# Proposed checks

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


# The task list


def task_numbers(tasks: Sequence[TaskState]) -> list[str]:
    """Outline numbers: "1", "2", … for tasks, and "2.1", "2.2", … for the fixes under task 2.

    Every fix in a chain (a fix of a fix, too) is numbered under the task that started it.
    """
    numbers: list[str] = []
    top_number: dict[str, str] = {}  # top-level task id -> its number
    top_of: dict[str, str] = {}  # task id -> id of the top-level task it belongs to
    fixes_so_far: Counter[str] = Counter()
    for state in tasks:
        if state.fixes is None:
            top = state.id
            top_number[top] = str(len(top_number) + 1)
            numbers.append(top_number[top])
        else:
            # A fix comes after the task it fixes, so that one is already placed.
            top = top_of[state.fixes]
            fixes_so_far[top] += 1
            numbers.append(f"{top_number[top]}.{fixes_so_far[top]}")
        top_of[state.id] = top
    return numbers


def task_label(state: TaskState, number: str, frame: str) -> Text:
    """A row: number, status and title, with the settings lined up under the title.

    Fix tasks are indented under the task they belong to. A running task shows the
    spinner ``frame``.
    """
    settings = state.task.settings
    head = f"{number}. " if state.fixes is None else f"   {number} "
    icon = frame if state.status == "running" else STATUS_ICONS[state.status]
    # Long prompts (like fix tasks) are titled by their first line.
    title = split_prompt(state.task.prompt)[0]
    label = Text(f"{head}{icon} {title}\n")
    indent = " " * (len(head) + cell_len(icon) + 1)
    details = f"{indent}{model_label(settings)} · {settings.tool_access}"
    if state.attempt:
        details += f" · fix {state.attempt}/{state.max_attempts}"
    if state.usage != Usage():
        details += f" · {usage_label(state.usage)}"
    label.append(details, style="dim")
    return label


def split_prompt(prompt: str) -> tuple[str, str]:
    """The first line of ``prompt`` as the title, and the rest as the body.

    The body loses the blank lines around it and the trailing whitespace of its last line, but
    keeps its indentation; a single line has an empty body, and an empty prompt gives ("", "").
    """
    title, _, body = prompt.partition("\n")
    return title, _trim_body(body)


def join_prompt(title: str, body: str) -> str:
    """The prompt the editor saves: ``title`` stripped, a blank line, then ``body`` trimmed.

    The body loses only the blank lines around it and the trailing whitespace of its last
    line; a blank body gives just the stripped title.
    """
    body = _trim_body(body)
    if not body:
        return title.strip()
    return f"{title.strip()}\n\n{body}"


def _trim_body(body: str) -> str:
    """``body`` without the blank lines around it or its last line's trailing whitespace."""
    lines = body.split("\n")
    while lines and not lines[0].strip():
        del lines[0]
    return "\n".join(lines).rstrip()


def usage_label(usage: Usage) -> str:
    """Tokens and cost, shortly: ``1.2M tok · $0.84``."""
    return f"{_count_label(usage.tokens)} tok · ${usage.cost_usd:.2f}"


def _count_label(count: int) -> str:
    # Pick the unit by what the next smaller one would round to, so 999,950 reads "1.0M", not
    # "1000.0k".
    for size, suffix in ((1_000_000, "M"), (1_000, "k")):
        if round(count / (size // 1_000), 1) >= 1_000:
            return f"{count / size:.1f}{suffix}"
    return str(count)


# The verification panel


def step_lines(steps: Sequence[StepState], frame: str, elapsed: int, title: str = "") -> list[str]:
    """One line per step, under the ``title`` heading if there is one. The running one shows
    the spinner ``frame`` and the ``elapsed`` seconds it has been running."""
    return [title] * bool(title) + [
        f"{frame} {s.name} · {elapsed}s"
        if s.status == "running"
        else f"{STEP_ICONS[s.status]} {s.name}"
        for s in steps
    ]


# The usage panel

# Bar colours by the least percent they start at, most urgent first.
USAGE_COLOURS = ((90.0, "red"), (75.0, "yellow"), (0.0, "green"))
STALE_STYLE = "dim"


def usage_lines(event: UsageChanged, width: int, now: datetime) -> list[Text]:
    """The usage panel's rows: a header and a bar ``width`` cells wide for each limit, dimmed
    unless the numbers are current, then a line on the status if there's one to say."""
    stale = event.status != "ok"
    lines = [row for limit in event.limits for row in _limit_rows(limit, width, now, stale)]
    status = _usage_status(event, now)
    return lines + [Text(status, style=STALE_STYLE)] * bool(status)


def _limit_rows(limit: UsageLimit, width: int, now: datetime, stale: bool) -> list[Text]:
    header = f"{limit.label}  {limit.percent:.0f}%"
    if limit.resets_at is not None:
        header += f"  resets {reset_label(limit.resets_at, now)}"
    width = max(width, 1)
    filled = round(limit.percent / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    style = STALE_STYLE if stale else _usage_colour(limit.percent)
    return [Text(header, style=STALE_STYLE if stale else ""), Text(bar, style=style)]


def _usage_colour(percent: float) -> str:
    return next(colour for least, colour in USAGE_COLOURS if percent >= least)


def _usage_status(event: UsageChanged, now: datetime) -> str:
    match event.status:
        case "rate_limited":
            return _rate_limited(event.retry_at, now)
        case "unavailable":
            return ": ".join(filter(None, ("usage unavailable", event.detail)))
        case _:
            return "" if event.limits else "no usage limits reported"


def _rate_limited(retry_at: datetime | None, now: datetime) -> str:
    if retry_at is None:
        return "usage: rate limited"
    return f"usage: rate limited, retrying {reset_label(retry_at, now)}"


def reset_label(when: datetime, now: datetime) -> str:
    """``when`` in ``now``'s timezone: the time, like ``3:40pm``, on the same day as ``now``,
    or on another day with the weekday, like ``Mon 9:00am``."""
    local = when.astimezone(now.tzinfo)
    hour = local.hour % 12 or 12
    time = f"{hour}:{local.minute:02d}{'am' if local.hour < 12 else 'pm'}"
    return time if local.date() == now.date() else f"{local:%a} {time}"


# The agents pane


@dataclass(frozen=True)
class AgentRow:
    """A running agent: which, in what role, on which task, and the orbit tool it waits on."""

    agent_id: str
    role: str
    task_id: str
    waiting_on: str | None = None


def track_agents(rows: tuple[AgentRow, ...], event: Event) -> tuple[AgentRow, ...]:
    """The running agents after ``event``."""
    if isinstance(event, AgentStarted):
        return (*rows, AgentRow(event.agent_id, event.role, event.task_id))
    if isinstance(event, AgentExited):
        return tuple(row for row in rows if row.agent_id != event.agent_id)
    if isinstance(event, AgentRequest | AgentReply):
        return _waiting(rows, event.agent_id, getattr(event, "tool", None))
    return rows


def _waiting(rows: tuple[AgentRow, ...], agent: str, tool: str | None) -> tuple[AgentRow, ...]:
    return tuple(replace(r, waiting_on=tool) if r.agent_id == agent else r for r in rows)


def agent_line(row: AgentRow, number: str | None) -> str:
    """``row`` as a line; ``number`` is its task's number in the list, if it has one there."""
    if number is not None:
        task = f"task {number}"
    elif row.task_id == STARTUP_TASK:
        task = "base commit"
    elif row.task_id == PLAN_TASK:
        task = "planning"
    else:
        task = f"task {row.task_id}"
    doing = f"waiting on {row.waiting_on}" if row.waiting_on else "working"
    return f"{row.agent_id} {row.role} · {task} · {doing}"


def review_line(status: ReviewStatus | None) -> str:
    """Whether review is paused, for the agents pane; empty when there's no review."""
    if status is None:
        return ""
    if status.paused:
        return "review: paused"
    return "review: active" + (f" · {status.open_batches} open" if status.open_batches else "")


# Conversation tabs

ALL_TAB_LABEL = "All"
"""The fixed first tab, showing every conversation; it isn't one of the tracked tabs."""

PLANNER = "planner"
"""Key of the pinned planner tab, and of the planner conversation's transcript entries."""

ROLE_TABS: tuple[str, ...] = ("planner", "coder", "reviewer", "super-reviewer", "configurator")
"""The built-in roles, in strip order, that each have a pinned tab shared by all their
agents; a pinned tab's key and label are its role's name. Mirrors the backend's
``BUILT_IN_ROLE_NAMES``, which the TUI can't import; ``tests/test_view.py`` keeps them in
step."""


RUNNING_MARK = "●"
"""Leads a tab's label while its role is at work."""


@dataclass(frozen=True)
class ConversationTab:
    """A conversation tab, by ``key`` (a pinned role's name, or a running agent's id),
    whether it has output not yet looked at, and how many of its role's agents are at work
    (``running``). Only a pinned tab counts them: a temporary tab is there only while its
    agent runs, and the planner's count follows its busy lane, as the planner agent itself
    runs from the first prompt until the session ends."""

    key: str
    role: str
    unread: bool = False
    running: int = 0


_TabOutput = (
    AssistantMessage
    | ToolCallStarted
    | ToolCallFinished
    | AgentRequest
    | AgentReply
    | ProgressNoted
    | TaskGaveUp
    | ErrorOccurred
    | TurnFinished
)
"""Events that can add an entry to their agent's conversation; a turn's end only does when
it gets a note."""

_TAB_OUTPUT: tuple[type[_TabOutput], ...] = get_args(_TabOutput)
"""The classes in ``_TabOutput``, for ``isinstance``."""


PINNED_TABS = tuple(ConversationTab(role, role) for role in ROLE_TABS)
"""The built-in roles' tabs, there from the start and never removed."""


def track_roles(roles: Mapping[str, str], event: Event) -> Mapping[str, str]:
    """Which pinned role each agent started in, by agent id, after ``event``."""
    if isinstance(event, AgentStarted) and event.role in ROLE_TABS:
        return {**roles, event.agent_id: event.role}
    return roles


def tab_key(agent: str | None, roles: Mapping[str, str]) -> str | None:
    """Which tab ``agent``'s entries belong to: its role's, when ``roles`` has it pinned."""
    return None if agent is None else roles.get(agent, agent)


def task_tab_key(state: TaskState | None) -> str | None:
    """Which pinned tab the notes on a task (its header, verification results and merge
    notes) belong to: its role's, when the role has one. None, for All only, when the role
    has no pinned tab or the task isn't on the list (``state`` None)."""
    if state is None or state.task.role not in ROLE_TABS:
        return None
    return state.task.role


def track_tabs(
    tabs: tuple[ConversationTab, ...],
    event: Event,
    active: str | None = None,
    roles: Mapping[str, str] = MappingProxyType({}),
) -> tuple[ConversationTab, ...]:
    """The open conversation tabs after ``event``; output for a tab other than ``active``,
    the one being shown, marks it unread. Agents in a pinned role share its tab, and
    ``roles`` says which role those agents are in."""
    if isinstance(event, AgentStarted):
        return _open_tab(tabs, event)
    if isinstance(event, AgentExited):
        return _close_tab(tabs, event.agent_id, roles)
    if _is_tab_output(event):
        return mark_tab_unread(tabs, tab_key(event.agent_id, roles), active)
    return tabs


def _close_tab(
    tabs: tuple[ConversationTab, ...], agent: str, roles: Mapping[str, str]
) -> tuple[ConversationTab, ...]:
    """``tabs`` without ``agent``'s own tab; the pinned tabs stay, with one agent fewer at
    work on the tab of ``agent``'s role when ``roles`` has it pinned."""
    tabs = tuple(tab for tab in tabs if tab.key in ROLE_TABS or tab.key != agent)
    return _count_running(tabs, roles.get(agent), -1)


def _is_tab_output(event: Event) -> TypeGuard[_TabOutput]:
    """Whether ``event`` adds an entry to its agent's conversation; a turn's end only does
    when it gets a note."""
    if isinstance(event, TurnFinished):
        return turn_end_entry(event.reason) is not None
    return isinstance(event, _TAB_OUTPUT)


def _open_tab(
    tabs: tuple[ConversationTab, ...], event: AgentStarted
) -> tuple[ConversationTab, ...]:
    """``tabs`` with one for the started agent, unless its role has a pinned tab, which
    counts one more agent at work instead."""
    if event.role in ROLE_TABS:
        return _count_running(tabs, event.role, 1)
    return (*tabs, ConversationTab(event.agent_id, event.role))


def _count_running(
    tabs: tuple[ConversationTab, ...], role: str | None, change: int
) -> tuple[ConversationTab, ...]:
    """``tabs`` with ``change`` more agents at work on the pinned tab of ``role``, which is
    None for a role with no pinned tab. The planner's tab is left to ``mark_planner_busy``."""
    if role is None or role == PLANNER:
        return tabs
    return tuple(
        replace(tab, running=tab.running + change) if tab.key == role else tab for tab in tabs
    )


def mark_planner_busy(tabs: tuple[ConversationTab, ...], busy: bool) -> tuple[ConversationTab, ...]:
    """``tabs`` with the planner's tab at work while the planner is ``busy`` answering."""
    return tuple(replace(tab, running=int(busy)) if tab.key == PLANNER else tab for tab in tabs)


def mark_tab_unread(
    tabs: tuple[ConversationTab, ...], key: str | None, active: str | None
) -> tuple[ConversationTab, ...]:
    """``tabs`` with the tab keyed ``key`` unread, unless it's ``active``, the one being
    shown; nothing changes for None, an entry under All only."""
    return tabs if key == active else _set_unread(tabs, key, True)


def mark_tab_read(tabs: tuple[ConversationTab, ...], agent: str) -> tuple[ConversationTab, ...]:
    """``tabs`` with the tab keyed ``agent`` no longer unread."""
    return _set_unread(tabs, agent, False)


def _set_unread(
    tabs: tuple[ConversationTab, ...], agent: str | None, unread: bool
) -> tuple[ConversationTab, ...]:
    return tuple(replace(tab, unread=unread) if tab.key == agent else tab for tab in tabs)


def tab_labels(tabs: Sequence[ConversationTab]) -> list[str]:
    """Tabs named by role: the first of a role is just the role, later ones "coder 2", …;
    tabs with agents at work start with "● ", and unread tabs end in " *"."""
    seen: Counter[str] = Counter()
    labels: list[str] = []
    for tab in tabs:
        seen[tab.role] += 1
        count = seen[tab.role]
        label = tab.role if count == 1 else f"{tab.role} {count}"
        labels.append(_marked(label, tab))
    return labels


def _marked(label: str, tab: ConversationTab) -> str:
    """``label`` with ``tab``'s running and unread markers."""
    if tab.running:
        label = f"{RUNNING_MARK} {label}"
    return f"{label} *" if tab.unread else label
