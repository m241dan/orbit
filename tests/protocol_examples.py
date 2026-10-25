"""One example of every command and event, each in its envelope, with fields set where that's
meaningful."""

from datetime import UTC, datetime

from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    PAUSED_FOR_API_UNAVAILABLE,
    PAUSED_FOR_USAGE_LIMIT,
    PLANNER,
    PLANNER_LANE,
    REVIEW_ORIGIN,
    AcceptTask,
    ActivateSession,
    Activity,
    AddTask,
    AddWorkspace,
    AgentEntry,
    AgentExited,
    AgentRef,
    AgentReply,
    AgentRequest,
    AgentsChanged,
    AgentStarted,
    AssistantMessage,
    Attach,
    BusyChanged,
    CancelAgent,
    CancelTurn,
    Catalog,
    ChecksProposed,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    ConversationReset,
    DecideProposal,
    DeleteSession,
    DirectoryEntry,
    DirectoryListing,
    EditTask,
    ErrorOccurred,
    Event,
    EventEnvelope,
    FireTrigger,
    Hello,
    History,
    HistoryPage,
    ListDirectory,
    LogGap,
    MessageSent,
    NewSession,
    Notice,
    OpenRequest,
    OwnershipDeclined,
    OwnershipOffered,
    OwnershipTaken,
    ProgressNoted,
    ProposalDecided,
    ProposedSetup,
    ProposedStep,
    RemoveTask,
    RemoveWorkspace,
    RenameSession,
    ResetConversation,
    ResumeStorage,
    ReviewStatus,
    RunningAgent,
    RunTasks,
    SendMessage,
    SessionRef,
    SessionStateChanged,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    StartupChecked,
    StepState,
    TakeOwnership,
    TaskGaveUp,
    TaskMerged,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TriggerEntry,
    TurnFinished,
    TurnStarted,
    UpdateSettings,
    Usage,
    UsageChanged,
    UsageLimit,
    UsageTotal,
    UsageTotals,
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
    Watch,
    Welcome,
    WorkspaceCloning,
    WorkspaceEntry,
    WorkspaceUsage,
)

WHEN = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
SETTINGS = Settings(model="opus", tool_access="full", effort="high")
SPEC = TaskSpec(prompt="Fix the bug", settings=SETTINGS, role="reviewer")
USAGE = Usage(
    input_tokens=10,
    output_tokens=20,
    cache_creation_input_tokens=30,
    cache_read_input_tokens=40,
    cost_usd=0.25,
    turns=2,
    duration_ms=1500,
)
TASK = TaskState(
    id="t1",
    task=SPEC,
    status="unverified",
    fixes="t0",
    origin=REVIEW_ORIGIN,
    attempt=1,
    max_attempts=3,
    batch="b1",
    round=2,
    usage=USAGE,
    reason="Unit tests failed",
    reason_detail="1 failed",
)
SUMMARY = SessionSummary(
    id="s1",
    name="Morning",
    last_active=WHEN,
    counts={"completed": 2, "error": 1},
    usage=USAGE,
    current=True,
    locked=True,
    unreadable="corrupt",
    active=True,
    state="running",
    viewers=2,
    activity=(Activity(kind="task_run", task_id="t1"), Activity(kind="startup_check")),
    paused_until=WHEN,
    paused_for=PAUSED_FOR_USAGE_LIMIT,
    paused_runtime="claude",
)
STEP = StepState(name="Unit tests", status="failed")
REVIEW = ReviewStatus(paused=True, open_batches=2)
PROPOSAL = ChecksProposed(
    agent_id="agent-1",
    task_id="t1",
    steps=(ProposedStep(name="Lint", command=("ruff", "check")),),
    setup=ProposedSetup(command=("uv", "sync"), watch=("uv.lock",)),
)


def command(payload: Command) -> CommandEnvelope:
    """``payload`` in a command envelope with every field set."""
    return CommandEnvelope(
        workspace="w1",
        traceparent="00-trace-span-01",
        tracestate="k=v",
        command_id="c1",
        session="s1",
        payload=payload,
    )


def event(payload: Event) -> EventEnvelope:
    """``payload`` in an event envelope with every field set."""
    return EventEnvelope(
        workspace="w1",
        traceparent="00-trace-span-01",
        tracestate="k=v",
        seq=7,
        session="s1",
        payload=payload,
    )


COMMANDS: tuple[CommandEnvelope, ...] = (
    command(Hello(client="tui", protocol_version=2, since=4)),
    command(SendMessage(agent=PLANNER, text="hello")),
    command(CancelTurn(lane=PLANNER_LANE)),
    command(UpdateSettings(settings=SETTINGS)),
    command(UpdateSettings(role="architect", settings=SETTINGS)),
    command(AddTask(task=SPEC)),
    command(EditTask(task_id="t1", task=SPEC)),
    command(RunTasks()),
    command(FireTrigger(trigger="super-review", input={"focus": "tests"})),
    command(AcceptTask(task_id="t1")),
    command(RemoveTask(task_id="t1")),
    command(CancelAgent(agent_id="agent-1")),
    command(DecideProposal(task_id="t1", action="revise", feedback="Add mypy")),
    command(Attach(workspace="w1", session="s1")),
    command(ActivateSession(session="s1")),
    command(NewSession(name="Evening")),
    command(RenameSession(session_id="s1", name="Night")),
    command(DeleteSession(session_id="s1")),
    command(ResetConversation(agent=PLANNER)),
    command(History(session="s1", before_seq=101, limit=50)),
    command(ResumeStorage()),
    command(TakeOwnership(take=True)),
    command(AddWorkspace(path="/src/orbit")),
    command(AddWorkspace(url="git@github.com:o/orbit.git")),
    command(RemoveWorkspace(workspace="w1")),
    command(RemoveWorkspace(workspace="w1", purge=True)),
    command(Watch(streams=(CONTROL_PLANE_STREAM,))),
    command(ListDirectory()),
    command(ListDirectory(path="/home/me/src")),
)

EVENTS: tuple[EventEnvelope, ...] = (
    event(SettingsChanged(settings=SETTINGS, model_choices=("opus", "sonnet"))),
    event(
        SettingsChanged(
            settings=SETTINGS,
            model_choices=("opus", "fast"),
            model_ids={"opus": "opus", "fast": "claude-haiku-5-5"},
        )
    ),
    event(SettingsChanged(role="architect", settings=SETTINGS)),
    event(TurnStarted(turn_id="turn-1", agent_id="agent-1")),
    event(AssistantMessage(turn_id="turn-1", agent_id="agent-1", text="Done")),
    event(
        ToolCallStarted(
            turn_id="turn-1",
            agent_id="agent-1",
            call_id="call-1",
            name="Bash",
            arguments={"command": "ls", "timeout": 5, "flags": ["-a"], "env": None},
        )
    ),
    event(
        ToolCallFinished(
            turn_id="turn-1", agent_id="agent-1", call_id="call-1", output="x", is_error=True
        )
    ),
    event(TurnFinished(turn_id="turn-1", agent_id="agent-1", reason="max_steps", usage=USAGE)),
    event(BusyChanged(busy=True, lane=PLANNER_LANE)),
    event(TasksChanged(tasks=(TASK,))),
    event(TaskStarted(task_id="t1")),
    event(VerificationChanged(steps=(STEP,), title="Startup check")),
    event(StartupChecked(passed=False, step="Unit tests", output="1 failed")),
    event(VerificationStarted(task_id="t1", step=2)),
    event(VerificationSkipped(task_id="t1", reason="no_changes")),
    event(VerificationFinished(task_id="t1", step=2, passed=True)),
    event(ErrorOccurred(turn_id="turn-1", agent_id="agent-1", code="internal", message="boom")),
    event(ErrorOccurred(code="git_failed", message="Couldn't clone x: denied", url="x")),
    event(AgentStarted(agent_id="agent-1", role="coder", task_id="t1")),
    event(
        AgentRequest(
            agent_id="agent-1", request_id="r1", tool="complete_task", arguments={"summary": "x"}
        )
    ),
    event(AgentReply(agent_id="agent-1", request_id="r1", text="No", is_error=True)),
    event(ProgressNoted(agent_id="agent-1", task_id="t1", note="Reading")),
    event(TaskGaveUp(agent_id="agent-1", task_id="t1", reason="Contradicts itself")),
    event(AgentExited(agent_id="agent-1", reason="crashed")),
    event(TaskMerged(task_id="t1", result="conflict", detail="README.md", commit="abc123")),
    event(Notice(message="Heads up")),
    event(REVIEW),
    event(PROPOSAL),
    event(
        ProposalDecided(
            command_id="c1", agent_id="agent-1", task_id="t1", action="revise", feedback="Add mypy"
        )
    ),
    event(
        UsageChanged(
            limits=(UsageLimit(label="Week", percent=42.5, resets_at=WHEN),),
            status="rate_limited",
            detail="Too many requests",
            retry_at=WHEN,
            runtime="claude",
        )
    ),
    event(MessageSent(command_id="c1", agent=PLANNER, text="hello")),
    event(ConversationReset(agent=PLANNER)),
    event(LogGap(first_seq=1204, last_seq=1210)),
    event(OwnershipOffered()),
    event(OwnershipTaken()),
    event(OwnershipDeclined()),
    event(
        SessionStateChanged(
            session="s1",
            active=True,
            state="running",
            viewers=2,
            activity=(
                Activity(kind="task_run", task_id="t1"),
                Activity(kind="waiting_for_coder", position=2, busy=4),
                Activity(kind="review", task_id="t2"),
                Activity(kind="planner"),
            ),
            paused_until=WHEN,
            paused_for=PAUSED_FOR_API_UNAVAILABLE,
            paused_runtime="claude",
        )
    ),
    event(
        Snapshot(
            session=SUMMARY,
            settings=SETTINGS,
            model_choices=("opus",),
            model_ids={"opus": "opus"},
            busy={"work": True, PLANNER_LANE: False},
            role_settings={PLANNER: SETTINGS, "architect": Settings(effort="max")},
            tasks=(TASK,),
            steps=(STEP,),
            steps_title="Task t1",
            review=REVIEW,
            proposals=(PROPOSAL,),
            agents=(
                RunningAgent(
                    agent_id="agent-1",
                    role="coder",
                    task_id="t1",
                    open_requests=(OpenRequest(request_id="r1", tool="complete_task"),),
                ),
            ),
            transcript=(event(AssistantMessage(turn_id="turn-1", text="Done.")),),
            storage_halt="Orbit stopped this session: saving the event log failed.",
            read_only=True,
            ownership_offered=True,
            active=False,
            open_session=SessionRef(id="s2", name="Today"),
            viewers=2,
        )
    ),
    event(
        HistoryPage(
            session="s1",
            events=(event(AssistantMessage(turn_id="turn-1", text="Earlier.")),),
            more=True,
        )
    ),
    event(
        Catalog(
            workspaces=(
                WorkspaceEntry(
                    id="w1", label="Orbit", path="/src/orbit", read_only=True, sessions=(SUMMARY,)
                ),
            ),
            agents=(
                AgentEntry(name=PLANNER, label="Planner", conversational=True, lane=PLANNER_LANE),
                AgentEntry(
                    name="auditor",
                    label="auditor",
                    conversational=False,
                    lane="audit",
                    model="opus",
                    effort="high",
                    personal=True,
                    runtime="claude",
                    usage_known=False,
                ),
            ),
            triggers=(TriggerEntry(name="super-review", label="Super review", manual=True),),
            lanes=("work", PLANNER_LANE, "audit"),
        )
    ),
    event(
        WorkspaceCloning(
            url="git@github.com:o/orbit.git", name="orbit", phase="Receiving objects", percent=42
        )
    ),
    event(
        WorkspaceCloning(
            url="git@github.com:o/orbit.git",
            name="orbit",
            phase="Done",
            percent=100,
            workspace="w1",
        )
    ),
    event(
        AgentsChanged(
            agents=(
                AgentRef(
                    workspace="w1", session="s1", agent_id="agent-1", role="coder", task_id="t1"
                ),
            )
        )
    ),
    event(
        UsageTotals(
            workspaces=(WorkspaceUsage(workspace="w1", today=USAGE, lifetime=USAGE + USAGE),),
            total=UsageTotal(today=USAGE, lifetime=USAGE + USAGE),
        )
    ),
    event(
        DirectoryListing(
            path="/home/me/src",
            parent="/home/me",
            entries=(
                DirectoryEntry(name="orbit", is_repository=True, workspace="w1", hidden=False),
                DirectoryEntry(name=".cache", is_repository=False, hidden=True),
                DirectoryEntry(name="docs", is_repository=False, hidden=False, target="/srv/docs"),
            ),
            truncated=True,
            total=2001,
        )
    ),
    event(Welcome(command_id="c1", server_version="0.1.0")),
    event(CommandAccepted(command_id="c1")),
    event(CommandRefused(command_id="c1", code="busy", message="Running")),
)

EXAMPLES: tuple[CommandEnvelope | EventEnvelope, ...] = COMMANDS + EVENTS
"""Every command and event, each in its envelope."""
