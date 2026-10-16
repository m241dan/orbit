from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orbit.protocol import (
    AcceptTask,
    AddTask,
    AgentExited,
    AgentReply,
    AgentRequest,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    CancelAgent,
    CancelTurn,
    EditTask,
    ErrorOccurred,
    ProgressNoted,
    RemoveTask,
    RequestSuperReview,
    ReviewStatus,
    RunTasks,
    Settings,
    SettingsChanged,
    StartupChecked,
    StepState,
    SubmitPrompt,
    TaskGaveUp,
    TasksChanged,
    TaskSpec,
    TaskStarted,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
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
    command_adapter,
    event_adapter,
)


def test_event_json_round_trip() -> None:
    event = ToolCallStarted(turn_id="t1", call_id="c1", name="read", arguments={"path": "x"})
    assert event_adapter.validate_json(event.model_dump_json()) == event


def test_command_dispatches_on_type() -> None:
    command = command_adapter.validate_json('{"type": "submit_prompt", "text": "hi"}')
    assert command == SubmitPrompt(text="hi")


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        command_adapter.validate_python({"type": "submit_prompt", "text": "hi", "extra": 1})


def test_rejects_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        SubmitPrompt(text="")


def test_settings_default_to_claude_default_model_and_restricted_tools() -> None:
    assert Settings() == Settings(model=None, tool_access="restricted")


def test_update_settings_round_trip() -> None:
    command = UpdateSettings(settings=Settings(model="opus", tool_access="full"))
    assert command_adapter.validate_json(command.model_dump_json()) == command


def test_settings_changed_round_trip() -> None:
    event = SettingsChanged(settings=Settings(tool_access="off"), model_choices=("opus", "sonnet"))
    assert event_adapter.validate_json(event.model_dump_json()) == event


def test_rejects_unknown_tool_access() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"tool_access": "sometimes"})


def test_task_commands_round_trip() -> None:
    task = TaskSpec(prompt="fix the bug", settings=Settings(model="opus", tool_access="full"))
    for command in (AddTask(task=task), EditTask(task_id="1", task=task), RunTasks()):
        assert command_adapter.validate_json(command.model_dump_json()) == command
    assert TaskSpec(prompt="write docs").settings == Settings()


def test_rejects_empty_task_prompt() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(prompt="")


def test_task_state_starts_pending() -> None:
    assert TaskState(id="1", task=TaskSpec(prompt="x")).status == "pending"
    assert TaskState(id="1", task=TaskSpec(prompt="x")).fixes is None
    assert StepState(name="Unit tests").status == "pending"


def test_state_and_log_events_round_trip() -> None:
    events = (
        BusyChanged(busy=True),
        TasksChanged(
            tasks=(
                TaskState(id="1", task=TaskSpec(prompt="one"), status="unverified"),
                TaskState(id="2", task=TaskSpec(prompt="fix it"), fixes="1"),
                TaskState(id="3", task=TaskSpec(prompt="three"), status="verifying"),
            )
        ),
        VerificationStarted(task_id="3", step=0),
        VerificationSkipped(task_id="2", reason="no_changes"),
        VerificationChanged(steps=(StepState(name="Lint", status="skipped"),)),
        TaskStarted(task_id="2"),
        VerificationChanged(steps=(StepState(name="Unit tests", status="failed"),)),
        VerificationFinished(task_id="1", step=0, passed=False),
    )
    for event in events:
        assert event_adapter.validate_json(event.model_dump_json()) == event


def test_rejects_unknown_task_status() -> None:
    with pytest.raises(ValidationError):
        TaskState.model_validate({"id": "1", "task": {"prompt": "x"}, "status": "sleeping"})


def test_task_spec_defaults_to_coder_role() -> None:
    assert TaskSpec(prompt="x").role == "coder"
    task = TaskSpec(prompt="review it", role="reviewer")
    assert command_adapter.validate_json(AddTask(task=task).model_dump_json()) == AddTask(task=task)


def test_rejects_empty_role() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(prompt="x", role="")


def test_task_state_defaults_to_manual_origin_and_no_attempts() -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="x"))
    assert (state.origin, state.attempt, state.max_attempts) == ("manual", 0, 0)


def test_rejects_unknown_task_origin() -> None:
    with pytest.raises(ValidationError):
        TaskState.model_validate({"id": "1", "task": {"prompt": "x"}, "origin": "magic"})


def test_task_state_with_origin_attempts_and_gave_up_round_trips() -> None:
    state = TaskState(
        id="4",
        task=TaskSpec(prompt="tidy"),
        status="gave_up",
        origin="review",
        attempt=2,
        max_attempts=5,
    )
    event = TasksChanged(tasks=(state,))
    assert event_adapter.validate_json(event.model_dump_json()) == event


def test_agent_events_round_trip() -> None:
    events = (
        AgentStarted(agent_id="agent-1", role="coder", task_id="1"),
        AgentRequest(
            agent_id="agent-1", request_id="r1", tool="complete_task", arguments={"summary": "done"}
        ),
        AgentReply(agent_id="agent-1", request_id="r1", text="Checks failed", is_error=True),
        ProgressNoted(agent_id="agent-1", task_id="1", note="tests written"),
        TaskGaveUp(agent_id="agent-1", task_id="1", reason="needs a decision"),
        AgentExited(agent_id="agent-1", reason="crashed"),
    )
    for event in events:
        assert event_adapter.validate_json(event.model_dump_json()) == event


def test_rejects_unknown_agent_exit_reason() -> None:
    with pytest.raises(ValidationError):
        AgentExited.model_validate({"agent_id": "a", "reason": "vanished"})


def test_turn_events_name_their_agent_when_one_runs_them() -> None:
    assert TurnStarted(turn_id="t").agent_id is None
    events = (
        TurnStarted(turn_id="t", agent_id="agent-1"),
        AssistantMessage(turn_id="t", text="hi", agent_id="agent-1"),
        ToolCallStarted(turn_id="t", call_id="c", name="Read", arguments={}, agent_id="agent-1"),
        ToolCallFinished(turn_id="t", call_id="c", output="", agent_id="agent-1"),
        TurnFinished(turn_id="t", reason="completed", agent_id="agent-1"),
        ErrorOccurred(turn_id="t", message="boom", agent_id="agent-1"),
    )
    for event in events:
        assert event_adapter.validate_json(event.model_dump_json()) == event


def test_task_state_carries_its_review_batch_and_round() -> None:
    state = TaskState(id="1", task=TaskSpec(prompt="x"), origin="review", batch="b", round=2)
    event = TasksChanged(tasks=(state,))
    assert event_adapter.validate_json(event.model_dump_json()) == event
    assert TaskState(id="1", task=TaskSpec(prompt="x")).round == 0


def test_request_super_review_round_trips() -> None:
    assert (
        command_adapter.validate_json(RequestSuperReview().model_dump_json())
        == RequestSuperReview()
    )


def test_planning_and_draft_messages_round_trip() -> None:
    for command in (
        AcceptTask(task_id="3"),
        RemoveTask(task_id="3"),
        SubmitPrompt(text="plan"),
        CancelTurn(lane="planner"),
    ):
        assert command_adapter.validate_json(command.model_dump_json()) == command
    event = BusyChanged(busy=True, lane="planner")
    assert event_adapter.validate_json(event.model_dump_json()) == event
    assert BusyChanged(busy=True).lane == "work"
    assert CancelTurn().lane == "work"
    with pytest.raises(ValidationError):
        SubmitPrompt(text="hi", to="conversation")  # type: ignore[call-arg]
    draft = TaskState(id="1", task=TaskSpec(prompt="x"), status="draft", origin="planner")
    assert TaskState.model_validate(draft.model_dump()) == draft


def test_agent_cancelling_and_review_status_round_trip() -> None:
    command = CancelAgent(agent_id="agent-2")
    assert command_adapter.validate_json(command.model_dump_json()) == command
    event = ReviewStatus(paused=True, open_batches=2)
    assert event_adapter.validate_json(event.model_dump_json()) == event


def test_startup_check_messages_round_trip() -> None:
    for event in (
        StartupChecked(passed=True),
        StartupChecked(passed=False, step="Lint", output="E501"),
        VerificationChanged(steps=(StepState(name="Lint"),), title="Startup check"),
    ):
        assert event_adapter.validate_json(event.model_dump_json()) == event
    assert VerificationChanged(steps=()).title == ""


def test_usage_adds_up_and_counts_every_kind_of_token() -> None:
    first = Usage(input_tokens=1, output_tokens=2, cost_usd=0.5, turns=1, duration_ms=10)
    second = Usage(cache_creation_input_tokens=3, cache_read_input_tokens=4, cost_usd=0.25)
    total = first + second
    assert total == Usage(
        input_tokens=1,
        output_tokens=2,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
        cost_usd=0.75,
        turns=1,
        duration_ms=10,
    )
    assert total.tokens == 10
    assert Usage().tokens == 0


def test_turns_and_tasks_carry_usage_when_known() -> None:
    assert TurnFinished(turn_id="t", reason="completed").usage is None
    assert TaskState(id="1", task=TaskSpec(prompt="x")).usage == Usage()
    usage = Usage(input_tokens=5, cost_usd=0.1)
    event = TurnFinished(turn_id="t", reason="completed", usage=usage)
    assert event_adapter.validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1)


def test_usage_changed_round_trips_with_its_times() -> None:
    resets = datetime(2026, 10, 16, 15, 40, tzinfo=UTC)
    event = UsageChanged(
        limits=(UsageLimit(label="Session", percent=52.0, resets_at=resets),),
        status="rate_limited",
        retry_at=datetime(2026, 10, 16, 12, 5, tzinfo=UTC),
    )
    assert event_adapter.validate_json(event.model_dump_json()) == event
    assert UsageChanged() == UsageChanged(limits=(), status="ok", detail="", retry_at=None)
    with pytest.raises(ValidationError):
        UsageLimit(label="Week", percent=101)
