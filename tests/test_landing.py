"""Landing finished work: committing it in the coder's tree and merging it into the base."""

import asyncio

from orbit.agent.application.ports import Merge
from orbit.agent.domain.agent_task import Limits
from orbit.protocol import (
    ErrorOccurred,
    Event,
    MergeResult,
    Notice,
    TaskMerged,
    TaskSpec,
    TurnEndReason,
)
from tests.agent_fakes import Call, FakeTree, Steps, board, run, session_for, verified


async def claims_done(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("complete_task", summary="Add the --fast flag")
    return "completed"


async def keeps_claiming(call: Call) -> TurnEndReason:
    await call("get_task")
    while "another agent" not in (await call("complete_task", summary="try")).text:
        pass
    return "completed"


async def gives_up(call: Call) -> TurnEndReason:
    await call("get_task")
    await call("give_up", reason="blocked")
    return "completed"


def statuses(events: list[Event]) -> list[str]:
    return [t.status for t in board(events)]


async def test_finished_work_is_done_in_the_tree_then_committed_and_merged() -> None:
    tree = FakeTree()
    steps = Steps(True)
    session, _, launcher = session_for(claims_done, verification=verified(steps), tree=tree)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="Make it faster.")), 5)

    assert tree.prepared == [False]
    assert launcher.launches[0].cwd == tree.path
    assert steps.roots == [tree.path]
    [message] = tree.commits
    assert message.startswith("Add the --fast flag\n\n")
    assert "Make it faster." in message
    assert TaskMerged(task_id="1", result="merged") in events
    assert statuses(events) == ["completed"]


async def test_work_that_did_not_pass_is_not_committed() -> None:
    tree = FakeTree()
    session, _, _ = session_for(gives_up, tree=tree)
    events = await asyncio.wait_for(run(session), 5)
    assert tree.commits == []
    assert not any(isinstance(e, TaskMerged) for e in events)
    assert statuses(events) == ["gave_up"]


async def test_a_fallback_fix_continues_from_the_failed_work() -> None:
    tree = FakeTree()
    session, _, _ = session_for(
        keeps_claiming,
        claims_done,
        verification=verified(Steps(False, False, True)),
        limits=Limits(max_attempts=1),
        tree=tree,
    )
    events = await asyncio.wait_for(run(session), 5)
    assert tree.prepared == [False, True]
    assert len(tree.commits) == 1
    assert statuses(events) == ["completed", "completed"]


async def test_work_rebased_onto_a_moved_base_is_verified_again_before_merging() -> None:
    tree = FakeTree(Merge("rebased"), Merge("merged"))
    steps = Steps(True, True)
    session, _, _ = session_for(claims_done, verification=verified(steps), tree=tree)
    events = await asyncio.wait_for(run(session), 5)
    assert steps.runs == ["Unit tests", "Unit tests"]
    assert TaskMerged(task_id="1", result="merged") in events
    assert statuses(events) == ["completed"]


async def test_rebased_work_that_fails_verification_falls_back() -> None:
    tree = FakeTree(Merge("rebased"))
    session, _, launcher = session_for(
        claims_done, claims_done, verification=verified(Steps(True, False, True)), tree=tree
    )
    events = await asyncio.wait_for(run(session), 5)
    first, fix = board(events)
    assert fix.fixes == first.id
    assert len(launcher.launches) == 2
    assert tree.prepared == [False, True]
    assert statuses(events) == ["completed", "completed"]


async def test_work_that_cannot_be_merged_stays_completed_with_the_reason() -> None:
    results: tuple[MergeResult, ...] = ("conflict", "blocked", "detached")
    for result in results:
        tree = FakeTree(Merge(result, "why it didn't merge"))
        session, _, _ = session_for(claims_done, tree=tree)
        events = await asyncio.wait_for(run(session), 5)
        merged = TaskMerged(task_id="1", result=result, detail="why it didn't merge")
        assert merged in events
        assert statuses(events) == ["completed"]


async def test_a_tree_that_cannot_be_prepared_fails_the_task() -> None:
    tree = FakeTree(broken="Setting up the coder's tree failed: no network")
    session, _, launcher = session_for(claims_done, claims_done, tree=tree)
    events = await asyncio.wait_for(run(session, TaskSpec(prompt="a"), TaskSpec(prompt="b")), 5)
    assert launcher.launches == []
    assert statuses(events) == ["error", "error"]
    assert ErrorOccurred(message="Setting up the coder's tree failed: no network") in events


async def test_leftovers_from_earlier_runs_are_announced_once_at_the_start() -> None:
    tree = FakeTree(leftovers=("2 commits on orbit/coder not yet merged into main.",))
    session, _, _ = session_for(tree=tree)
    stream = session.events()
    first = [await anext(stream), await anext(stream)]
    assert Notice(message="2 commits on orbit/coder not yet merged into main.") in first
    # A second listener isn't told again.
    waiting = asyncio.ensure_future(anext(session.events()))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    waiting.cancel()


async def test_without_a_tree_tasks_work_in_the_current_directory() -> None:
    steps = Steps(True)
    session, _, launcher = session_for(claims_done, verification=verified(steps))
    await asyncio.wait_for(run(session), 5)
    assert launcher.launches[0].cwd == "."
    assert steps.roots == ["."]


async def test_a_base_that_keeps_moving_leaves_the_work_unmerged() -> None:
    tree = FakeTree(*[Merge("rebased")] * 4)
    steps = Steps(True, True, True, True)
    session, _, _ = session_for(claims_done, verification=verified(steps), tree=tree)
    events = await asyncio.wait_for(run(session), 5)
    [merged] = [e for e in events if isinstance(e, TaskMerged)]
    assert merged.result == "rebased" and "kept moving" in merged.detail
    assert len(steps.runs) == 4  # The claim, then once after each rebase.
    assert statuses(events) == ["completed"]


async def test_without_verification_rebased_work_is_merged_straight_away() -> None:
    tree = FakeTree(Merge("rebased"), Merge("merged"))
    session, _, _ = session_for(claims_done, tree=tree)
    events = await asyncio.wait_for(run(session), 5)
    assert TaskMerged(task_id="1", result="merged") in events
