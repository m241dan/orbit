"""One example of every command and event, each in its envelope, with fields set where that's
meaningful."""

from datetime import UTC, datetime

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    REVIEW_ORIGIN,
    AcceptTask,
    AddTask,
    AgentEntry,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
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
    EditTask,
    ErrorOccurred,
    Event,
    EventEnvelope,
    FireTrigger,
    Hello,
    ListSessions,
    NewSession,
    Notice,
    OpenRequest,
    OpenSession,
    ProgressNoted,
    ProposedSetup,
    ProposedStep,
    RemoveTask,
    RenameSession,
    ResetConversation,
    ReviewStatus,
    RunningAgent,
    RunTasks,
    SendMessage,
    SessionsListed,
    SessionSummary,
    Settings,
    SettingsChanged,
    Snapshot,
    StartupChecked,
    StepState,
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
    VerificationChanged,
    VerificationFinished,
    VerificationSkipped,
    VerificationStarted,
    Welcome,
    WorkspaceEntry,
)

WHEN = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
SETTINGS = Settings(model="opus", tool_access="full")
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
    command(Hello(client="tui", protocol_version=1, since=4)),
    command(SendMessage(agent=PLANNER, text="hello")),
    command(CancelTurn(lane=PLANNER_LANE)),
    command(UpdateSettings(settings=SETTINGS)),
    command(AddTask(task=SPEC)),
    command(EditTask(task_id="t1", task=SPEC)),
    command(RunTasks()),
    command(FireTrigger(trigger="super-review", input={"focus": "tests"})),
    command(AcceptTask(task_id="t1")),
    command(RemoveTask(task_id="t1")),
    command(CancelAgent(agent_id="agent-1")),
    command(DecideProposal(task_id="t1", action="revise", feedback="Add mypy")),
    command(ListSessions()),
    command(OpenSession(session_id="s1")),
    command(NewSession(name="Evening")),
    command(RenameSession(session_id="s1", name="Night")),
    command(DeleteSession(session_id="s1")),
    command(ResetConversation(agent=PLANNER)),
)

EVENTS: tuple[EventEnvelope, ...] = (
    event(SettingsChanged(settings=SETTINGS, model_choices=("opus", "sonnet"))),
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
        UsageChanged(
            limits=(UsageLimit(label="Week", percent=42.5, resets_at=WHEN),),
            status="rate_limited",
            detail="Too many requests",
            retry_at=WHEN,
        )
    ),
    event(SessionsListed(sessions=(SUMMARY,), reason="refreshed")),
    event(ConversationReset(agent=PLANNER)),
    event(
        Snapshot(
            session=SUMMARY,
            settings=SETTINGS,
            model_choices=("opus",),
            busy={"work": True, PLANNER_LANE: False},
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
        )
    ),
    event(
        Catalog(
            workspaces=(WorkspaceEntry(id="w1", label="Orbit", sessions=(SUMMARY,)),),
            agents=(AgentEntry(name=PLANNER, label="Planner", conversational=True),),
            triggers=(TriggerEntry(name="super-review", label="Super review", manual=True),),
            lanes=("work", PLANNER_LANE),
        )
    ),
    event(Welcome(command_id="c1", server_version="0.1.0")),
    event(CommandAccepted(command_id="c1")),
    event(CommandRefused(command_id="c1", code="busy", message="Running")),
)

EXAMPLES: tuple[CommandEnvelope | EventEnvelope, ...] = COMMANDS + EVENTS
"""Every command and event, each in its envelope."""
