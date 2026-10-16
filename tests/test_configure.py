"""Accepted checks: orbit saves them to the project config, lands them, and uses them live."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orbit.agent import (
    NO_STEPS_NOTE,
    AgentSession,
    GitCoderTree,
    Verification,
    read_config,
    write_checks,
)
from orbit.agent.application.ports import Merge, ProposalDecision
from orbit.agent.domain.commits import configure_message
from orbit.agent.domain.roles import ChecksProposal, Setup
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.config_file import CONFIG_PATH
from orbit.protocol import (
    AddTask,
    BusyChanged,
    ErrorOccurred,
    Event,
    MergeResult,
    Notice,
    RunTasks,
    StepState,
    TaskMerged,
    TaskSpec,
    TurnEndReason,
    VerificationChanged,
    VerificationSkipped,
)
from tests.agent_fakes import STEP, Call, FakeTree, Script, Steps, board, session_for, verified
from tests.test_proposals import CONFIGURE, LINT, SETUP, TESTS, Review, proposes

CHECKS = ChecksProposal(
    steps=(VerificationStep("Lint", ("ruff", "check")), VerificationStep("Tests", ("pytest",))),
    setup=Setup(command=("uv", "sync"), watch=("uv.lock",)),
)
CODE = TaskSpec(prompt="Add a flag.")


class Writer:
    """Records the checks it's asked to write, and where."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, ChecksProposal]] = []

    def __call__(self, root: str, checks: ChecksProposal) -> None:
        self.writes.append((root, checks))


def unsaveable(root: str, checks: ChecksProposal) -> None:
    raise RuntimeError("Couldn't save the checks to .orbit/config.toml: read-only")


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Added the flag")
    return "completed"


async def everything(session: AgentSession, *tasks: TaskSpec) -> list[Event]:
    """Queue ``tasks`` and run them; every event until the session goes idle."""
    for task in tasks:
        await session.send(AddTask(task=task))
    await session.send(RunTasks())
    events: list[Event] = []
    async for event in session.events():
        events.append(event)
        if event == BusyChanged(busy=False):
            return events
    raise AssertionError("unreachable")


def accepting(*scripts: Script, steps: Steps, **options: Any) -> AgentSession:
    """A session whose configurator proposes ``CHECKS``, which the user accepts; ``scripts``
    play the agents of the tasks after it."""
    session, _, _ = session_for(
        proposes({"steps": [LINT, TESTS], "setup": SETUP}, replies=[]),
        *scripts,
        verification=verified(steps),
        proposal_review=Review(ProposalDecision("accept")),
        **options,
    )
    return session


async def test_accepted_checks_are_written_into_the_tree_and_committed() -> None:
    tree, writer = FakeTree(), Writer()
    session = accepting(steps=Steps(True, True, True), tree=tree, write_config=writer)
    events = await asyncio.wait_for(everything(session, CONFIGURE), 5)
    assert writer.writes == [(FakeTree.path, CHECKS)]
    assert tree.commits == [configure_message(CHECKS)]
    assert tree.commits[0].startswith("Configure verification steps\n\n")
    assert "- Lint: ruff check\n- Tests: pytest" in tree.commits[0]
    assert TaskMerged(task_id="1", result="merged", commit="c1") in events
    [task] = board(events)
    assert task.status == "completed"


@pytest.mark.parametrize("result", ["conflict", "blocked", "detached"])
async def test_accepted_checks_land_like_task_work(result: MergeResult) -> None:
    tree = FakeTree(Merge(result, "in the way"))
    session = accepting(steps=Steps(True, True, True), tree=tree, write_config=Writer())
    events = await asyncio.wait_for(everything(session, CONFIGURE), 5)
    [merged] = [e for e in events if isinstance(e, TaskMerged)]
    assert (merged.result, merged.detail) == (result, "in the way")


async def test_a_rebase_is_checked_with_the_accepted_steps() -> None:
    tree = FakeTree(Merge("rebased"))
    steps = Steps(True, True, True, True, True)
    session = accepting(steps=steps, tree=tree, write_config=Writer())
    events = await asyncio.wait_for(everything(session, CONFIGURE), 5)
    assert steps.runs == ["setup", "Lint", "Tests", "Lint", "Tests"]
    assert TaskMerged(task_id="1", result="merged", commit="c1") in events


async def test_without_a_worktree_they_are_written_here_and_not_committed() -> None:
    writer = Writer()
    session = accepting(steps=Steps(True, True, True), write_config=writer)
    events = await asyncio.wait_for(everything(session, CONFIGURE), 5)
    assert writer.writes == [(".", CHECKS)]
    assert not any(isinstance(e, TaskMerged) for e in events)


async def test_the_next_task_is_verified_by_the_accepted_steps_and_setup() -> None:
    tree, steps = FakeTree(), Steps(True, True, True, True, True)
    session = accepting(claims_done, steps=steps, tree=tree)
    events = await asyncio.wait_for(everything(session, CONFIGURE, CODE), 5)
    assert steps.runs == ["setup", "Lint", "Tests", "Lint", "Tests"]
    assert tree.setups == [CHECKS.setup]
    assert VerificationChanged(steps=(StepState(name="Lint"), StepState(name="Tests"))) in events
    assert [t.status for t in board(events)] == ["completed", "completed"]


async def test_checks_that_cant_be_saved_end_the_task_in_error_and_change_nothing() -> None:
    tree, steps = FakeTree(), Steps(True, True, True, True)
    session = accepting(claims_done, steps=steps, tree=tree, write_config=unsaveable)
    events = await asyncio.wait_for(everything(session, CONFIGURE, CODE), 5)
    message = "Couldn't save the checks to .orbit/config.toml: read-only"
    assert ErrorOccurred(message=message) in events
    assert [t.status for t in board(events)] == ["error", "completed"]
    assert steps.runs == ["setup", "Lint", "Tests", STEP.name]
    assert tree.setups == []
    assert len(tree.commits) == 1  # Only the next task's work landed.
    shown = [e.steps for e in events if isinstance(e, VerificationChanged)]
    assert {tuple(step.name for step in steps) for steps in shown} == {(STEP.name,)}


async def test_accepted_checks_end_the_no_steps_state() -> None:
    steps = Steps(True, True, True, True, True)
    replies: list[str] = []
    session, _, _ = session_for(
        proposes({"steps": [LINT, TESTS], "setup": SETUP}, replies=replies),
        claims_done,
        verification=Verification((), steps),
        proposal_review=Review(ProposalDecision("accept")),
    )
    events = await asyncio.wait_for(everything(session, CONFIGURE, CODE), 5)
    assert Notice(message=NO_STEPS_NOTE) in events
    shown = [e.steps for e in events if isinstance(e, VerificationChanged)]
    assert shown[0] == (StepState(name="Lint"), StepState(name="Tests"))
    assert not any(isinstance(e, VerificationSkipped) for e in events)
    assert steps.runs == ["setup", "Lint", "Tests", "Lint", "Tests"]


async def test_coders_cannot_change_their_own_checks() -> None:
    replies: list[str] = []

    async def meddles(call: Call) -> TurnEndReason:
        await call("get_task")
        replies.append((await call("propose_checks", steps=[LINT])).text)
        await call("complete_task", summary="done")
        return "completed"

    writer, steps = Writer(), Steps(True, True)
    session, _, _ = session_for(
        meddles,
        claims_done,
        verification=verified(steps),
        proposal_review=Review(ProposalDecision("accept")),
        write_config=writer,
    )
    await asyncio.wait_for(everything(session, CODE, CODE), 5)
    assert replies == ["Your role (coder) can't use propose_checks."]
    assert writer.writes == []
    assert steps.runs == [STEP.name, STEP.name]


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


async def test_in_a_real_repository_the_config_lands_on_the_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".orbit").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / CONFIG_PATH).write_text("# Ours.\nfix_attempts = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    checks = {"steps": [LINT, TESTS], "setup": {"command": ["true"]}}
    session, _, _ = session_for(
        proposes(checks, replies=[]),
        verification=verified(Steps(True, True, True)),
        proposal_review=Review(ProposalDecision("accept")),
        tree=GitCoderTree(str(repo)),
        write_config=write_checks,
    )
    try:
        events = await asyncio.wait_for(everything(session, CONFIGURE), 20)
    finally:
        await session.close()
    [merged] = [e for e in events if isinstance(e, TaskMerged)]
    assert merged.result == "merged"
    assert git(repo, "log", "-1", "--format=%s", "main") == "Configure verification steps"
    assert (repo / CONFIG_PATH).read_text().startswith("# Ours.\nfix_attempts = 2\n")
    config = read_config(str(repo))
    assert config.fix_attempts == 2
    assert config.verification.steps == CHECKS.steps
    assert config.setup == Setup(command=("true",))


async def test_without_a_worktree_the_real_file_is_written_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    session, _, _ = session_for(
        proposes({"steps": [LINT]}, replies=[]),
        verification=verified(Steps(True)),
        proposal_review=Review(ProposalDecision("accept")),
        write_config=write_checks,
    )
    await asyncio.wait_for(everything(session, CONFIGURE), 5)
    assert read_config(str(tmp_path)).verification.steps == CHECKS.steps[:1]
    assert not (tmp_path / ".git").exists()
