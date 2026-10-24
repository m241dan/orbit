"""The workspaces ``orbit serve`` serves as frontends add and remove them: each found or
registered in the store, flagged served and locked while it's served."""

import asyncio
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import pytest
import typer

from orbit.agent import (
    ImportBlocked,
    OrbitConfig,
    PathKind,
    RepositoryUnreadable,
    ServedWorkspaces,
    SqliteStore,
    StorageSettings,
    StoreError,
    WorkspaceHost,
    WorkspaceLock,
    WorkspaceRegistry,
    WorkspaceUnavailable,
    orbit_dir,
    read_config,
)
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.external import served_workspaces
from orbit.cli import backend, served
from orbit.cli.backend import _Backend, _backend, _Directories
from orbit.cli.served import _added, _workspaces
from orbit.protocol import INTERNAL, INVALID, StartupChecked
from tests.agent_fakes import agent_exited, events_until, logged_with_traceback

FLAGS = _Backend(None, None, False, False, False, False, new=False, name=None)
CLONES = Path("/nonexistent/clones")
"""A clone directory for serving that clones nothing."""


def workspaces(stack: ExitStack, store: SqliteStore, flags: _Backend = FLAGS) -> ServedWorkspaces:
    return _workspaces(stack, flags, store, StorageSettings(), store.state_dir / "workspaces")


def directory(tmp_path: Path, name: str = "repo") -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def free(root: Path) -> bool:
    """Whether no orbit holds the workspace lock on the repository at ``root``."""
    lock = WorkspaceLock(orbit_dir(str(root)))
    owner = lock.take()
    lock.close()
    return owner is None


async def test_a_directory_a_file_and_nothing_are_told_apart(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    source = workspaces(stack, store)
    (tmp_path / "notes.txt").write_text("")

    assert await source.kind(str(directory(tmp_path))) == PathKind.DIRECTORY
    assert await source.kind(str(tmp_path / "notes.txt")) == PathKind.FILE
    assert await source.kind(str(tmp_path / "gone")) == PathKind.MISSING


async def test_a_directory_inside_a_repository_stands_for_its_top_level(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "docs").mkdir()
    source = workspaces(stack, store)

    assert await source.top_level(str(root / "docs")) == str(root.resolve())
    assert await source.top_level(str(tmp_path)) == str(tmp_path.resolve())


async def test_opening_a_workspace_flags_it_served_and_locks_it_until_closed(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    source.url = "ws://127.0.0.1:7433/"

    host = await source.open(str(root))
    registry = WorkspaceRegistry(store)
    served = await registry.served()
    owner = WorkspaceLock(root / ".orbit").owner()
    await host.close()
    await source.set_served(host.workspace_id, False)
    await source.close(host.workspace_id)

    assert served == [(host.workspace_id, str(root))]
    assert owner.url == "ws://127.0.0.1:7433/" and not host.read_only
    assert await registry.served() == [] and free(root)
    assert source.locks == ()


async def test_the_latest_added_is_the_workspace_opened_last_even_when_reopened(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    first, second = directory(tmp_path, "a"), directory(tmp_path, "b")
    source = workspaces(stack, store)

    before = await source.latest_added()
    host = await source.open(str(first))
    await (await source.open(str(second))).close()
    latest = await source.latest_added()
    await host.close()
    await source.set_served(host.workspace_id, False)
    await source.close(host.workspace_id)
    await (await source.open(str(first))).close()

    assert (before, latest) == (None, str(second))
    assert await source.latest_added() == str(first)


async def test_restoring_a_workspace_served_keeps_the_latest_added(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    first, second = directory(tmp_path, "a"), directory(tmp_path, "b")
    source = workspaces(stack, store)
    host = await source.open(str(first))
    await host.close()
    await (await source.open(str(second))).close()

    await source.set_served(host.workspace_id, False)
    await source.restore_served(host.workspace_id)
    served = await WorkspaceRegistry(store).served()

    assert await source.latest_added() == str(second)
    assert sorted(path for _, path in served) == [str(first), str(second)]


async def test_a_store_failing_to_find_the_latest_added_is_unavailable(
    stack: ExitStack, store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fails(self: WorkspaceRegistry) -> str | None:
        raise StoreError("The store failed.")

    monkeypatch.setattr(WorkspaceRegistry, "latest_added", fails)

    with pytest.raises(WorkspaceUnavailable, match="The store failed") as unavailable:
        await workspaces(stack, store).latest_added()
    assert unavailable.value.code == INTERNAL


async def test_a_workspace_another_orbit_owns_opens_read_only(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    other = WorkspaceLock(root / ".orbit")
    other.take()
    source = workspaces(stack, store)

    host = await source.open(str(root))
    await host.close()
    other.close()

    assert host.read_only


async def test_the_locks_still_held_are_let_go_as_the_stack_closes(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    host = await source.open(str(root))
    await host.close()
    held = not free(root)

    stack.close()

    assert held and free(root)


class CountedLock(WorkspaceLock):
    """A workspace lock that counts how often it's let go."""

    closes = 0

    def close(self) -> None:
        CountedLock.closes += 1
        super().close()


async def test_a_lock_closed_with_its_workspace_isnt_let_go_again_as_the_stack_closes(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(served_workspaces, "WorkspaceLock", CountedLock)
    monkeypatch.setattr(CountedLock, "closes", 0)
    root = directory(tmp_path)
    source = workspaces(stack, store)
    for _ in range(3):
        host = await source.open(str(root))
        await host.close()
        await source.close(host.workspace_id)

    stack.close()

    assert CountedLock.closes == 3


async def test_an_invalid_configuration_is_unavailable_saying_why(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    (root / ".orbit").mkdir()
    (root / ".orbit" / "config.toml").write_text("roles = 1\n")

    with pytest.raises(WorkspaceUnavailable, match=r"^Invalid orbit configuration: ") as refused:
        await workspaces(stack, store).open(str(root))
    assert refused.value.code == INVALID and free(root)


async def test_a_repository_git_has_no_identity_for_is_unavailable_with_a_worktree(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(served_workspaces, "has_git_identity", lambda path: False)
    flags = _Backend(None, None, False, True, False, False, new=False, name=None)

    with pytest.raises(
        WorkspaceUnavailable, match=f"^{re.escape(f'Git has no identity for commits in {root}')}"
    ) as refused:
        await workspaces(stack, store, flags).open(str(root))
    assert refused.value.code == INVALID and free(root)


async def test_a_slow_identity_check_leaves_the_event_loop_running(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    checking, gate = threading.Event(), threading.Event()

    def waits(path: str) -> bool:
        checking.set()
        assert gate.wait(10)
        return False

    monkeypatch.setattr(served_workspaces, "has_git_identity", waits)
    flags = _Backend(None, None, False, True, False, False, new=False, name=None)
    opening = asyncio.create_task(workspaces(stack, store, flags).open(str(root)))

    async def lets_go() -> None:
        """Let the identity check finish once it's under way, running meanwhile."""
        while not checking.is_set():
            await asyncio.sleep(0.01)
        gate.set()

    await asyncio.wait_for(lets_go(), 5)

    with pytest.raises(WorkspaceUnavailable, match=r"^Git has no identity"):
        await opening


async def test_a_lock_that_cant_be_made_is_unavailable_naming_it(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    (root / ".orbit").write_text("")

    with pytest.raises(
        WorkspaceUnavailable, match=r"^Couldn't lock the repository with "
    ) as refused:
        await workspaces(stack, store).open(str(root))
    assert refused.value.code == INTERNAL


async def test_a_repository_git_cant_read_is_unavailable_taking_no_lock_in_it(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    (root / ".git").write_text(f"gitdir: {tmp_path / 'nowhere'}\n")

    with pytest.raises(WorkspaceUnavailable, match=r"not a git repository") as refused:
        await workspaces(stack, store).open(str(root))
    assert refused.value.code == INTERNAL
    assert not (root / ".orbit").exists()


async def test_a_store_that_fails_is_unavailable_and_lets_the_lock_go(
    stack: ExitStack, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    store = await SqliteStore.open(tmp_path / "state")
    await store.close()

    with pytest.raises(WorkspaceUnavailable) as refused:
        await workspaces(stack, store).open(str(root))
    assert refused.value.code == INTERNAL and free(root)


@pytest.mark.parametrize(
    "failure",
    [
        StoreError("The store failed."),
        RecoveryFailed("/parked.jsonl", "it's unreadable"),
        RepositoryUnreadable("Git can't find the repository /repo is in."),
    ],
)
async def test_a_store_or_recovery_failing_partway_through_opening_is_unavailable_as_internal(
    failure: Exception,
    stack: ExitStack,
    store: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = directory(tmp_path)

    async def fails(store: SqliteStore, root: Path, *, owned: bool) -> None:
        raise failure

    monkeypatch.setattr(served_workspaces, "open_workspace_store", fails)

    with pytest.raises(WorkspaceUnavailable, match=f"^{re.escape(str(failure))}$") as refused:
        await workspaces(stack, store).open(str(root))
    assert refused.value.code == INTERNAL and free(root)


async def test_a_failure_opening_the_store_lets_the_lock_go(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)

    async def fails(store: SqliteStore, root: Path, *, owned: bool) -> None:
        raise RuntimeError("A bug.")

    monkeypatch.setattr(served_workspaces, "open_workspace_store", fails)

    with pytest.raises(RuntimeError, match="A bug"):
        await workspaces(stack, store).open(str(root))
    assert free(root)


async def test_the_configuration_is_read_off_the_event_loop(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    threads: list[threading.Thread] = []

    def recorded(path: str) -> OrbitConfig:
        threads.append(threading.current_thread())
        return read_config(path)

    monkeypatch.setattr(served_workspaces, "read_config", recorded)

    host = await workspaces(stack, store).open(str(root))
    await host.close()

    assert threads and threads[0] is not threading.main_thread()


async def test_a_workspace_elsewhere_is_configured_verified_and_worked_in_at_its_root(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    seen = tmp_path / "seen"
    code = f"import os, pathlib; pathlib.Path({str(seen)!r}).write_text(os.getcwd())"
    (root / ".orbit").mkdir()
    (root / ".orbit" / "config.toml").write_text(
        f"[[verification.steps]]\nname = 'Where'\ncommand = [{sys.executable!r}, '-c', {code!r}]\n"
    )
    verifying = _Backend(None, None, True, False, False, False, new=False, name=None)

    host = await workspaces(stack, store, verifying).open(str(root))
    events = await events_until(host, agent_exited("startup-check"))
    await host.close()

    assert Path.cwd() != root
    assert host.verification is not None and host.agents is not None
    assert [step.name for step in host.verification.steps] == ["Where"]
    assert StartupChecked(passed=True) in events
    assert seen.read_text() == str(root)
    assert host.agents.root == str(root)


async def test_a_failure_making_the_host_lets_the_lock_go(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)

    def fails(*args: object, **kwargs: object) -> None:
        raise RuntimeError("A bug.")

    monkeypatch.setattr(backend, "_agents", fails)

    with pytest.raises(RuntimeError, match="A bug"):
        await workspaces(stack, store).open(str(root))
    assert free(root) and await WorkspaceRegistry(store).served() == []


async def test_a_workspace_whose_unflagging_fails_is_unavailable_and_stays_served_and_locked(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    host = await source.open(str(root))
    await host.close()

    async def fails(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        raise StoreError("The store failed.")

    monkeypatch.setattr(WorkspaceRegistry, "set_served", fails)

    with pytest.raises(WorkspaceUnavailable, match="The store failed") as unavailable:
        await source.set_served(host.workspace_id, False)
    held = not free(root) and source.serves(host.workspace_id)
    stack.close()

    assert unavailable.value.code == INTERNAL
    assert held and free(root)


async def unflagging_failed(
    source: ServedWorkspaces, root: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The id of the workspace at ``root``, opened with ``source``, its host closed, whose
    unflagging then failed, so its lock is still held, with the store working again."""
    host = await source.open(str(root))
    await host.close()
    set_served = WorkspaceRegistry.set_served

    async def fails(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        raise StoreError("The store failed.")

    monkeypatch.setattr(WorkspaceRegistry, "set_served", fails)
    with pytest.raises(WorkspaceUnavailable):
        await source.set_served(host.workspace_id, False)
    monkeypatch.setattr(WorkspaceRegistry, "set_served", set_served)
    return host.workspace_id


async def test_a_workspace_whose_unflagging_failed_is_added_again_owned_with_its_held_lock(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(served_workspaces, "WorkspaceLock", CountedLock)
    monkeypatch.setattr(CountedLock, "closes", 0)
    root = directory(tmp_path)
    source = workspaces(stack, store)
    workspace_id = await unflagging_failed(source, root, monkeypatch)
    held = source.locks

    host = await source.open(str(root))
    await host.close()
    served = await WorkspaceRegistry(store).served()
    stack.close()

    assert not host.read_only and host.workspace_id == workspace_id
    assert served == [(workspace_id, str(root))] and source.locks == ()
    assert len(held) == 1 and CountedLock.closes == 1 and free(root)


def test_a_workspace_whose_unflagging_failed_is_served_again_owned_and_freed_once_closed(
    stack: ExitStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    store = asyncio.run(SqliteStore.open(tmp_path / "state"))
    source = workspaces(stack, store)
    workspace_id = asyncio.run(unflagging_failed(source, root, monkeypatch))
    held = source.locks

    served, make = source.served(root)
    locks = source.locks
    asyncio.run(make().close())
    asyncio.run(source.close(workspace_id))
    asyncio.run(store.close())

    assert served.owner is None and locks == held and len(locks) == 1
    assert free(root) and source.locks == ()


def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with a commit, and a linked worktree of it, which shares its lock."""
    root = directory(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    commit = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q"]
    subprocess.run([*commit, "--allow-empty", "-m", "Start"], cwd=root, check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-q", str(linked)], cwd=root, check=True)
    return root, linked


async def test_a_linked_worktree_of_a_repository_served_owned_is_served_read_only(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root, linked = linked_worktree(tmp_path)
    source = workspaces(stack, store)
    first = await source.open(str(root))
    await first.close()

    second = await source.open(str(linked))
    await second.close()
    locks = source.locks
    stack.close()

    assert not first.read_only and second.read_only
    assert len(locks) == 2 and locks[0] is not locks[1]


async def test_removing_a_linked_worktree_leaves_the_repository_served_locked(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root, linked = linked_worktree(tmp_path)
    source = workspaces(stack, store)
    first = await source.open(str(root))
    await first.close()
    second = await source.open(str(linked))
    await second.close()

    await source.set_served(second.workspace_id, False)
    await source.close(second.workspace_id)
    held = not free(root) and source.serves(first.workspace_id)
    stack.close()

    assert held and free(root)


async def test_a_failed_add_of_a_workspace_whose_lock_is_held_leaves_it_held(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    workspace_id = await unflagging_failed(source, root, monkeypatch)
    (root / ".orbit" / "config.toml").write_text("roles = 1\n")

    with pytest.raises(WorkspaceUnavailable, match=r"^Invalid orbit configuration: "):
        await source.open(str(root))
    held = not free(root) and source.serves(workspace_id)
    stack.close()

    assert held and free(root)


async def test_closing_a_workspace_lets_its_lock_go_leaving_its_flag(
    stack: ExitStack, store: SqliteStore, tmp_path: Path
) -> None:
    root = directory(tmp_path)
    source = workspaces(stack, store)
    host = await source.open(str(root))
    await host.close()

    await source.close(host.workspace_id)

    assert free(root) and not source.serves(host.workspace_id)
    assert await WorkspaceRegistry(store).served() == [(host.workspace_id, str(root))]


def flagged(state: Path) -> list[str]:
    """The directories of the workspaces flagged served in the store in ``state``."""

    async def read() -> list[str]:
        store = await SqliteStore.open(state)
        try:
            return [path for _, path in await WorkspaceRegistry(store).served()]
        finally:
            await store.close()

    return asyncio.run(read())


def test_serving_with_every_workspace_added_servable_flags_them_all(tmp_path: Path) -> None:
    first, second = directory(tmp_path, "first"), directory(tmp_path, "second")
    state = tmp_path / "state"

    with served._serving(FLAGS, [first, second], state, tmp_path / "config", CLONES) as serving:
        roots = [workspace.root for workspace in serving.workspaces]

    assert roots == [first, second]
    assert sorted(flagged(state)) == [str(first), str(second)]


def test_serving_stops_with_a_later_workspace_added_unservable_leaving_the_others_unflagged(
    stack: ExitStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = directory(tmp_path, "good"), directory(tmp_path, "bad")
    invalid_config(bad, monkeypatch)
    state = tmp_path / "state"

    with pytest.raises(typer.Exit) as exited:
        stack.enter_context(served._serving(FLAGS, [good, bad], state, tmp_path / "config", CLONES))

    assert exited.value.exit_code == 2
    assert flagged(state) == [] and free(good)


def test_serving_stops_if_the_workspaces_added_cant_be_flagged(
    stack: ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = directory(tmp_path)

    async def fails(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        raise StoreError("The store failed.")

    monkeypatch.setattr(WorkspaceRegistry, "set_served", fails)

    with pytest.raises(typer.Exit) as exited:
        stack.enter_context(
            served._serving(FLAGS, [root], tmp_path / "state", tmp_path / "config", CLONES)
        )

    assert exited.value.exit_code == 2
    assert capsys.readouterr().err == "The store failed.\n" and free(root)


def flagging_the_second_fails(
    monkeypatch: pytest.MonkeyPatch, *, unflagging: bool
) -> list[tuple[str, bool]]:
    """The flags set as flagging the second workspace added fails, and unflagging fails too
    if ``unflagging``."""
    set_served = WorkspaceRegistry.set_served
    calls: list[tuple[str, bool]] = []

    async def second_fails(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        calls.append((workspace_id, served))
        if served and len(calls) == 2:
            raise StoreError("The store failed.")
        if not served and unflagging:
            raise StoreError("The store failed again.")
        await set_served(self, workspace_id, served)

    monkeypatch.setattr(WorkspaceRegistry, "set_served", second_fails)
    return calls


def test_serving_stops_if_a_later_workspace_added_cant_be_flagged_unflagging_the_others(
    stack: ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = directory(tmp_path, "first"), directory(tmp_path, "second")
    state = tmp_path / "state"
    calls = flagging_the_second_fails(monkeypatch, unflagging=False)

    with pytest.raises(typer.Exit) as exited:
        stack.enter_context(
            served._serving(FLAGS, [first, second], state, tmp_path / "config", CLONES)
        )

    assert exited.value.exit_code == 2
    assert [served for _, served in calls] == [True, True, False]
    assert calls[0][0] == calls[2][0]
    assert capsys.readouterr().err == "The store failed.\n"
    assert flagged(state) == [] and free(first) and free(second)


def test_a_failure_unflagging_the_others_is_logged_and_the_flags_failure_reported(
    stack: ExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = directory(tmp_path, "first"), directory(tmp_path, "second")
    state = tmp_path / "state"
    flagging_the_second_fails(monkeypatch, unflagging=True)

    with pytest.raises(typer.Exit) as exited:
        stack.enter_context(
            served._serving(FLAGS, [first, second], state, tmp_path / "config", CLONES)
        )

    assert exited.value.exit_code == 2
    assert capsys.readouterr().err == "The store failed.\n"
    message = f"Orbit failed to flag {first} not served: The store failed again."
    assert logged_with_traceback(caplog.records, message)
    assert flagged(state) == [str(first)]


type Refusal = Callable[[Path, pytest.MonkeyPatch], _Backend]
"""What makes the workspace at a root unservable, giving the flags it's served with."""


def invalid_config(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Backend:
    (root / ".orbit").mkdir()
    (root / ".orbit" / "config.toml").write_text("roles = 1\n")
    return FLAGS


def missing_identity(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Backend:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(served_workspaces, "has_git_identity", lambda path: False)
    return _Backend(None, None, False, True, False, False, new=False, name=None)


def unmakeable_lock(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Backend:
    (root / ".orbit").write_text("")
    return FLAGS


@pytest.mark.parametrize("refusal", [invalid_config, missing_identity, unmakeable_lock])
def test_serve_startup_and_a_frontend_add_refuse_a_workspace_saying_the_same(
    refusal: Refusal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = directory(tmp_path)
    flags = refusal(root, monkeypatch)
    store = asyncio.run(SqliteStore.open(tmp_path / "state"))
    with ExitStack() as stack:
        source = workspaces(stack, store, flags)
        with pytest.raises(typer.Exit) as exited:
            _added(source, root)
        with pytest.raises(WorkspaceUnavailable) as refused:
            asyncio.run(source.open(str(root)))
    asyncio.run(store.close())

    assert exited.value.exit_code == 2
    assert capsys.readouterr().err == f"{refused.value}\n"


def gated(monkeypatch: pytest.MonkeyPatch, gate: threading.Event, fails: bool = False) -> None:
    """Have opening a workspace wait on ``gate`` as it reads the configuration, then fail if
    ``fails``."""

    def waits(path: str) -> OrbitConfig:
        assert gate.wait(10)
        if fails:
            raise RuntimeError("A bug.")
        return read_config(path)

    monkeypatch.setattr(served_workspaces, "read_config", waits)


async def cancelled_while_preparing(
    source: ServedWorkspaces, root: Path, gate: threading.Event
) -> None:
    """Open the workspace at ``root`` with ``source``, cancel it while it's being prepared, then
    let the preparation finish and the cancellation end."""
    opening = asyncio.create_task(source.open(str(root)))
    await asyncio.sleep(0.05)
    opening.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await opening


async def test_an_add_cancelled_while_preparing_lets_the_lock_go_once_prepared(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate)
    source = workspaces(stack, store)

    await cancelled_while_preparing(source, root, gate)

    assert free(root) and source.locks == ()
    assert await WorkspaceRegistry(store).served() == []


async def test_a_later_add_of_a_workspace_whose_add_was_cancelled_takes_its_lock(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate)
    source = workspaces(stack, store)
    await cancelled_while_preparing(source, root, gate)

    host = await source.open(str(root))
    await host.close()

    assert not host.read_only


async def test_an_add_cancelled_while_preparing_that_then_fails_takes_no_lock(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate, fails=True)
    source = workspaces(stack, store)

    await cancelled_while_preparing(source, root, gate)

    assert free(root) and source.locks == ()


async def test_an_add_cancelled_as_it_flags_the_workspace_served_leaves_it_unflagged_and_free(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    flagged = asyncio.Event()
    set_served = WorkspaceRegistry.set_served

    async def flags_then_waits(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        await set_served(self, workspace_id, served)
        if served:
            flagged.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(WorkspaceRegistry, "set_served", flags_then_waits)
    source = workspaces(stack, store)
    opening = asyncio.create_task(source.open(str(root)))
    await flagged.wait()
    opening.cancel()

    with pytest.raises(asyncio.CancelledError):
        await opening
    assert free(root) and source.locks == ()
    assert await WorkspaceRegistry(store).served() == []


async def test_an_add_cancelled_as_it_flags_served_whose_unflagging_fails_is_still_cancelled(
    stack: ExitStack,
    store: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = directory(tmp_path)
    flagging = asyncio.Event()

    async def waits_or_fails(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        if not served:
            raise StoreError("The store failed.")
        flagging.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(WorkspaceRegistry, "set_served", waits_or_fails)
    source = workspaces(stack, store)
    opening = asyncio.create_task(source.open(str(root)))
    await flagging.wait()
    opening.cancel()

    with pytest.raises(asyncio.CancelledError):
        await opening
    message = f"Orbit failed to flag {root} not served: The store failed."
    assert logged_with_traceback(caplog.records, message)
    assert free(root) and source.locks == ()


async def opened_as_the_server_starts(
    source: ServedWorkspaces, root: Path, gate: threading.Event, url: str
) -> WorkspaceHost:
    """The host of the workspace at ``root``, opened with ``source`` before the server's
    ``url`` is known and prepared once it is."""
    opening = asyncio.create_task(source.open(str(root)))
    await asyncio.sleep(0.05)
    source.url = url
    gate.set()
    return await opening


async def test_a_lock_taken_as_the_server_starts_names_its_url(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate)
    source = workspaces(stack, store)

    host = await opened_as_the_server_starts(source, root, gate, "ws://127.0.0.1:7433/")
    await host.close()

    assert WorkspaceLock(root / ".orbit").owner().url == "ws://127.0.0.1:7433/"


async def test_a_lock_that_cant_name_the_url_known_once_taken_is_unavailable_and_let_go(
    stack: ExitStack, store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate)

    def unwritable(lock: WorkspaceLock, url: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(WorkspaceLock, "announce", unwritable)
    source = workspaces(stack, store)

    with pytest.raises(
        WorkspaceUnavailable, match=r"^Couldn't write the workspace lock "
    ) as refused:
        await opened_as_the_server_starts(source, root, gate, "ws://127.0.0.1:7433/")
    assert refused.value.code == INTERNAL
    assert free(root) and source.locks == ()
    assert await WorkspaceRegistry(store).served() == []


async def test_a_lock_that_cant_name_the_url_whose_unflagging_fails_is_unavailable_saying_why(
    stack: ExitStack,
    store: SqliteStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate)
    set_served = WorkspaceRegistry.set_served

    def unwritable(lock: WorkspaceLock, url: str) -> None:
        raise OSError("disk full")

    async def unflagging_fails(self: WorkspaceRegistry, workspace_id: str, served: bool) -> None:
        if not served:
            raise StoreError("The store failed.")
        await set_served(self, workspace_id, served)

    monkeypatch.setattr(WorkspaceLock, "announce", unwritable)
    monkeypatch.setattr(WorkspaceRegistry, "set_served", unflagging_fails)
    source = workspaces(stack, store)

    with pytest.raises(
        WorkspaceUnavailable, match=r"^Couldn't write the workspace lock .*: disk full$"
    ) as refused:
        await opened_as_the_server_starts(source, root, gate, "ws://127.0.0.1:7433/")
    assert refused.value.code == INTERNAL
    message = f"Orbit failed to flag {root} not served: The store failed."
    assert logged_with_traceback(caplog.records, message)
    assert free(root) and source.locks == ()


def test_a_lock_served_from_the_start_that_cant_name_the_url_leaves_the_flag_alone(
    stack: ExitStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    gate = threading.Event()
    gated(monkeypatch, gate)

    def unwritable(lock: WorkspaceLock, url: str) -> None:
        raise OSError("disk full")

    store = asyncio.run(SqliteStore.open(tmp_path / "state"))
    source = workspaces(stack, store)
    gate.set()
    workspace_id = asyncio.run(flagged_and_closed(source, root))
    gate.clear()
    monkeypatch.setattr(WorkspaceLock, "announce", unwritable)
    serving = threading.Thread(target=lambda: refused_served(source, root))
    serving.start()
    time.sleep(0.05)
    source.url = "ws://127.0.0.1:7433/"
    gate.set()
    serving.join()
    served = asyncio.run(WorkspaceRegistry(store).served())
    asyncio.run(store.close())

    assert free(root) and source.locks == ()
    assert served == [(workspace_id, str(root))]


async def flagged_and_closed(source: ServedWorkspaces, root: Path) -> str:
    """The id of the workspace at ``root``, opened with ``source``, so flagged served, then
    closed, leaving its flag."""
    host = await source.open(str(root))
    await host.close()
    await source.close(host.workspace_id)
    return host.workspace_id


def refused_served(source: ServedWorkspaces, root: Path) -> None:
    """Serve the workspace at ``root`` from the start with ``source``, refused as unavailable."""
    with pytest.raises(WorkspaceUnavailable, match=r"^Couldn't write the workspace lock "):
        source.served(root)


def failing_with(failure: Exception) -> Refusal:
    """What has opening the workspace's view of the store fail with ``failure``."""

    def failing(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Backend:
        async def fails(store: SqliteStore, root: Path, *, owned: bool) -> None:
            raise failure

        monkeypatch.setattr(served_workspaces, "open_workspace_store", fails)
        return FLAGS

    return failing


failing_store = failing_with(StoreError("The store failed."))
failing_import = failing_with(ImportBlocked("Couldn't import /sessions: it's unreadable."))
failing_recovery = failing_with(RecoveryFailed("/parked.jsonl", "it's unreadable"))


def chat_directories(tmp_path: Path, root: Path) -> _Directories:
    return _Directories(root, tmp_path / "state", tmp_path / "config")


@pytest.mark.parametrize(
    "refusal", [invalid_config, unmakeable_lock, failing_store, failing_import, failing_recovery]
)
def test_chat_and_a_frontend_add_refuse_a_workspace_saying_the_same_and_let_the_lock_go(
    refusal: Refusal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = directory(tmp_path)
    flags = refusal(root, monkeypatch)
    with pytest.raises(typer.Exit) as exited:
        _backend(flags, chat_directories(tmp_path, root), pick_session=False)
    chat_free = refusal is unmakeable_lock or free(root)
    store = asyncio.run(SqliteStore.open(tmp_path / "state"))
    with ExitStack() as stack, pytest.raises(WorkspaceUnavailable) as refused:
        asyncio.run(workspaces(stack, store, flags).open(str(root)))
    asyncio.run(store.close())

    assert exited.value.exit_code == 2 and chat_free
    assert capsys.readouterr().err == f"{refused.value}\n"


def test_chat_opens_a_repository_git_has_no_identity_for_unflagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = directory(tmp_path)
    flags = missing_identity(root, monkeypatch)

    chat = _backend(flags, chat_directories(tmp_path, root), pick_session=False)
    try:
        held = not free(root)
        served = asyncio.run(WorkspaceRegistry(chat.store).served())
    finally:
        asyncio.run(chat.store.close())
        chat.lock.close()

    assert held and served == [] and chat.owner is None
