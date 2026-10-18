import asyncio
import fcntl
import os
import pstats
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Coroutine, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from orbit import __version__
from orbit.agent import (
    MODEL_ALIASES,
    AgentSession,
    ClaudeCliRunner,
    ConfigError,
    FallbackFiles,
    GitCoderTree,
    Limits,
    McpHost,
    Role,
    Setup,
    SqliteStore,
    StorageSettings,
    UsageApi,
    Verification,
    VerificationStep,
    run_step,
    workspace_state,
)
from orbit.agent.application.ports import UsageSource
from orbit.agent.domain.reviews import ReviewLimits
from orbit.agent.domain.sessions import SessionRecord, Unreadable, default_name, record_json
from orbit.cli import app
from orbit.protocol import (
    EventEnvelope,
    Notice,
    SessionsListed,
    Settings,
    Snapshot,
    ThreadedSession,
    encode,
)
from tests.agent_fakes import greeted, queued, startup_of, store_of

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


class FakeApp:
    """Starts its session, which builds the backend on its thread, and closes it."""

    # The backend sessions built, the threads they were built on and their usage sources.
    launched: ClassVar[list[AgentSession]] = []
    threads: ClassVar[list[str]] = []
    usage_sources: ClassVar[list[UsageSource | None]] = []
    # The files in profile_dir while each app ran async.
    profile_dir: ClassVar[Path] = Path()
    profiling: ClassVar[list[list[str]]] = []
    # What each app does with the backend session while it runs, if anything.
    during: ClassVar[Callable[[AgentSession], Coroutine[Any, Any, None]] | None] = None
    # Why each app's session is halted as it exits, if it is.
    halted: ClassVar[str | None] = None

    def __init__(self, session: ThreadedSession) -> None:
        self.session = session
        self.storage_halt = FakeApp.halted

    def run(self) -> None:
        asyncio.run(self._open_and_close())

    async def run_async(self) -> None:
        await self._open_and_close()

    async def _open_and_close(self) -> None:
        self.session.start()
        if FakeApp.profile_dir.exists():
            FakeApp.profiling.append(sorted(p.name for p in FakeApp.profile_dir.iterdir()))
        if FakeApp.during is not None:
            await self._on_backend(FakeApp.during(FakeApp.launched[-1]))
        await self.session.close()

    async def _on_backend(self, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` on the backend's loop, where its session lives, and wait for it."""
        backend = self.session._backend
        assert backend is not None
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work, backend.loop))


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[AgentSession]:
    def session(*args: Any, usage: UsageSource | None = None, **kwargs: Any) -> AgentSession:
        built = AgentSession(*args, usage=usage, **kwargs)
        FakeApp.launched.append(built)
        FakeApp.threads.append(threading.current_thread().name)
        FakeApp.usage_sources.append(usage)
        return built

    monkeypatch.setattr("orbit.cli.OrbitApp", FakeApp)
    monkeypatch.setattr("orbit.cli.AgentSession", session)
    FakeApp.launched = []
    FakeApp.threads = []
    FakeApp.usage_sources = []
    FakeApp.profile_dir = tmp_path / "profile"
    FakeApp.profiling = []
    FakeApp.during = None
    FakeApp.halted = None
    return FakeApp.launched


@pytest.fixture(autouse=True)
def restore_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Chat changes into its workspace; put the working directory back afterwards. Each test
    starts in an empty directory, so chat never imports the sessions of the repository the tests
    run in."""
    monkeypatch.chdir(Path.cwd())
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The state directory chat keeps its store in, unless told otherwise."""
    path = tmp_path / "state"
    monkeypatch.setenv("ORBIT_STATE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The config directory chat reads the user's config from, unless told otherwise."""
    path = tmp_path / "config"
    monkeypatch.setenv("ORBIT_CONFIG_DIR", str(path))
    return path


@dataclass(frozen=True)
class Greeted:
    """What a chat's session publishes once greeted, before anything runs: its ``notices``, and
    whether it ``listed`` the saved sessions."""

    notices: list[str]
    listed: bool


@pytest.fixture
def greetings(launched: list[AgentSession]) -> list[Greeted]:
    """What each chat's session publishes once greeted, while its app runs."""
    seen: list[Greeted] = []

    async def greet_it(session: AgentSession) -> None:
        await greeted(session)
        events = queued(session)
        notices = [e.message for e in events if isinstance(e, Notice)]
        seen.append(Greeted(notices, any(isinstance(e, SessionsListed) for e in events)))

    FakeApp.during = greet_it
    return seen


def test_chat_works_in_the_current_directory_by_default(
    greetings: list[Greeted], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert Path.cwd() == tmp_path.resolve()
    [greeting] = greetings
    assert f"Workspace: {tmp_path.resolve()}" in greeting.notices


@pytest.mark.parametrize("flag", ["-C", "--workspace"])
def test_chat_works_in_the_workspace_it_is_given(
    launched: list[AgentSession],
    greetings: list[Greeted],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    project = tmp_path / "project"
    (project / ".orbit").mkdir(parents=True)
    (project / ".orbit" / "config.toml").write_text("fix_attempts = 3\nmax_checks = 4\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = runner.invoke(app, ["chat", flag, str(project)])

    assert result.exit_code == 0
    assert Path.cwd() == project.resolve()
    [session] = launched
    [greeting] = greetings
    # The project's own configuration is read, from the workspace.
    assert session.agents is not None
    assert session.agents.limits == Limits(max_attempts=3, max_checks=4)
    assert f"Workspace: {project.resolve()}" in greeting.notices


def test_a_workspace_inside_a_git_repository_is_its_top_level(
    launched: list[AgentSession], greetings: list[Greeted], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    result = runner.invoke(app, ["chat", "-C", str(sub)])

    assert result.exit_code == 0
    assert Path.cwd() == repo.resolve()
    [session] = launched
    [greeting] = greetings
    assert session.agents is not None
    assert isinstance(session.agents.tree, GitCoderTree)
    assert f"Workspace: {repo.resolve()}" in greeting.notices


def test_a_workspace_outside_git_is_used_as_given(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    plain = tmp_path / "plain" / "sub"
    plain.mkdir(parents=True)
    result = runner.invoke(app, ["chat", "-C", str(plain)])

    assert result.exit_code == 0
    assert Path.cwd() == plain.resolve()
    [session] = launched
    assert session.agents is not None and session.agents.tree is None


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with a commit."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(
        path,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    return path


WHEN = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)
SAVED = SessionRecord(id="abc", name="auth rework", created=WHEN, last_active=WHEN)


def legacy_sessions(directory: Path) -> Path:
    """An older orbit's sessions folder in ``directory``, holding ``SAVED``."""
    sessions = directory / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "abc.json").write_text(record_json(SAVED))
    return sessions


def store_in(session: AgentSession) -> SqliteStore:
    store = store_of(session)
    assert isinstance(store, SqliteStore)
    return store


def store_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("orbit-sqlite")]


async def stored_ids(state: Path, workspace: Path) -> list[str]:
    """The ids of the sessions saved in ``state`` for ``workspace``."""
    store = await SqliteStore.open(state, str(workspace))
    try:
        return [stored.id for stored in await store.sessions()]
    finally:
        await store.close()


def test_chat_keeps_sessions_in_the_store_in_the_state_directory(
    launched: list[AgentSession], state: Path, tmp_path: Path
) -> None:
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0

    [session] = launched
    assert store_in(session)._state_dir == state
    assert (state / "orbit.db").exists()
    assert not (tmp_path / ".orbit").exists()
    assert store_threads() == []  # The store was closed once the app ended.


def test_chat_parks_unsaved_events_in_the_state_directory_else_the_workspace(
    launched: list[AgentSession], state: Path, tmp_path: Path
) -> None:
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0

    [session] = launched
    writer = session._events._writer
    assert writer is not None and isinstance(writer._fallback, FallbackFiles)
    workspace_id = store_in(session).workspace_id
    assert workspace_id
    assert writer._fallback.places("s1") == [
        state / "fallback" / f"{workspace_id}-s1.jsonl",
        tmp_path.resolve() / ".orbit" / "fallback" / "s1.jsonl",
    ]


def test_the_state_directory_option_wins_over_the_environment(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat", "--state-dir", "elsewhere"]).exit_code == 0

    [session] = launched
    assert store_in(session)._state_dir == tmp_path / "elsewhere"
    assert (tmp_path / "elsewhere" / "orbit.db").exists()


def test_without_a_user_config_chat_keeps_the_default_storage_settings(
    launched: list[AgentSession],
) -> None:
    assert runner.invoke(app, ["chat"]).exit_code == 0

    [session] = launched
    assert session._storage == StorageSettings()


def test_chat_reads_the_storage_settings_in_the_config_directory(
    launched: list[AgentSession], config_dir: Path
) -> None:
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[storage]\ntool_output_kb = 1\n\n[server]\n")
    assert runner.invoke(app, ["chat"]).exit_code == 0

    [session] = launched
    assert session._storage == StorageSettings(tool_output_kb=1)
    assert session._events._cut_bytes == 1024


def test_the_config_directory_option_wins_over_the_environment(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    (tmp_path / "start" / "mine").mkdir()
    (tmp_path / "start" / "mine" / "config.toml").write_text("[storage]\nsession_log_mb = 5\n")
    workspace = tmp_path / "project"
    workspace.mkdir()
    # A relative directory is taken from where orbit started, not the workspace.
    result = runner.invoke(app, ["chat", "--config-dir", "mine", "-C", str(workspace)])

    assert result.exit_code == 0
    [session] = launched
    assert session._storage == StorageSettings(session_log_mb=5)


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("tool_output = 3", "storage.tool_output"),
        ("tool_output_kb = -1", "storage.tool_output_kb"),
        ('tool_output_kb = "64"', "storage.tool_output_kb"),
    ],
)
def test_an_invalid_storage_setting_stops_chat_naming_the_file_and_the_key(
    launched: list[AgentSession], config_dir: Path, line: str, key: str
) -> None:
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text(f"[storage]\n{line}\n")
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 2
    assert f"Invalid orbit configuration: {path}: {key}: " in result.stderr
    assert launched == []


def test_chat_imports_an_older_orbits_session_files(
    greetings: list[Greeted], repo: Path, state: Path
) -> None:
    legacy_sessions(repo / ".git" / "orbit")
    assert runner.invoke(app, ["chat", "-C", str(repo)]).exit_code == 0

    assert asyncio.run(stored_ids(state, repo)) == ["abc"]
    assert (repo / ".git" / "orbit" / "sessions.imported" / "abc.json").exists()
    assert not (repo / ".git" / "orbit" / "sessions").exists()
    [greeting] = greetings
    assert greeting.notices[-1:] == [
        "Imported 1 saved session from .git/orbit/sessions; the old folder is now"
        " sessions.imported."
    ]


def test_chat_imports_old_files_beside_an_earlier_import_to_the_next_free_name(
    greetings: list[Greeted], repo: Path, state: Path
) -> None:
    earlier = legacy_sessions(repo / ".git" / "orbit").rename(
        repo / ".git" / "orbit" / "sessions.imported"
    )
    legacy_sessions(repo / ".git" / "orbit")
    assert runner.invoke(app, ["chat", "-C", str(repo)]).exit_code == 0

    assert (earlier / "abc.json").exists()
    assert (repo / ".git" / "orbit" / "sessions.imported-2" / "abc.json").exists()
    [greeting] = greetings
    assert greeting.notices[-1].endswith("; the old folder is now sessions.imported-2.")


def test_the_import_note_names_a_folder_outside_the_workspace_in_full(
    launched: list[AgentSession], greetings: list[Greeted], repo: Path, tmp_path: Path
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked))
    legacy_sessions(repo / ".git" / "orbit")
    assert runner.invoke(app, ["chat", "-C", str(linked), "--no-worktree"]).exit_code == 0

    [greeting] = greetings
    sessions = repo.resolve() / ".git" / "orbit" / "sessions"
    assert f"Imported 1 saved session from {sessions}; " in greeting.notices[-1]


def test_chat_without_old_session_files_has_no_import_note(
    greetings: list[Greeted], tmp_path: Path
) -> None:
    assert runner.invoke(app, ["chat", "-C", str(tmp_path), "--no-verify"]).exit_code == 0

    [greeting] = greetings
    assert greeting.notices == [f"Workspace: {tmp_path.resolve()}"]


def parked_notice(seq: int) -> EventEnvelope:
    return EventEnvelope(seq=seq, session="s1", payload=Notice(message=f"parked {seq}"))


async def workspace_id_of(state: Path, workspace: Path) -> str:
    store = await SqliteStore.open(state, str(workspace))
    try:
        return store.workspace_id
    finally:
        await store.close()


async def logged_in(state: Path, workspace: Path) -> list[EventEnvelope]:
    """The session ``s1``'s events in the store in ``state`` for ``workspace``."""
    store = await SqliteStore.open(state, str(workspace))
    try:
        return await store.recent("s1", 100)
    finally:
        await store.close()


def park(state: Path, workspace: Path, *seqs: int) -> Path:
    """A fallback file of the session ``s1`` in ``state`` holding notices numbered ``seqs``."""
    workspace_id = asyncio.run(workspace_id_of(state, workspace.resolve()))
    path = state / "fallback" / f"{workspace_id}-s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{encode(parked_notice(seq))}\n" for seq in seqs))
    return path


@dataclass(frozen=True)
class Order:
    """Where the Snapshot and each notice came in what a chat's session published first."""

    snapshot: int
    notices: dict[str, int]


@pytest.fixture
def orders(launched: list[AgentSession]) -> list[Order]:
    """Where the Snapshot and the notices come in what each chat's session publishes first."""
    seen: list[Order] = []

    async def greet_it(session: AgentSession) -> None:
        await greeted(session)
        events = queued(session)
        snapshot = next(n for n, event in enumerate(events) if isinstance(event, Snapshot))
        notices = {e.message: n for n, e in enumerate(events) if isinstance(e, Notice)}
        seen.append(Order(snapshot, notices))

    FakeApp.during = greet_it
    return seen


def test_chat_recovers_parked_events_and_notes_it_after_the_snapshot(
    orders: list[Order], state: Path, tmp_path: Path
) -> None:
    path = park(state, tmp_path, 2, 1)
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0

    assert asyncio.run(logged_in(state, tmp_path.resolve())) == [parked_notice(1), parked_notice(2)]
    assert not path.exists()
    [order] = orders
    shown = path.relative_to(tmp_path)  # The state directory is in the workspace here.
    note = f"Recovered 2 events from {shown} into the session's log."
    assert order.notices[note] > order.snapshot


def test_a_parked_file_in_the_workspace_is_recovered_and_named_within_it(
    greetings: list[Greeted], state: Path, tmp_path: Path
) -> None:
    path = tmp_path / ".orbit" / "fallback" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(f"{encode(parked_notice(1))}\n")
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0

    assert asyncio.run(logged_in(state, tmp_path.resolve())) == [parked_notice(1)]
    [greeting] = greetings
    assert greeting.notices[-1] == (
        "Recovered 1 event from .orbit/fallback/s1.jsonl into the session's log."
    )


def test_events_recovered_twice_are_stored_once(
    greetings: list[Greeted], state: Path, tmp_path: Path
) -> None:
    park(state, tmp_path, 1, 2)
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0
    park(state, tmp_path, 2, 3)
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0

    logged = asyncio.run(logged_in(state, tmp_path.resolve()))
    assert logged == [parked_notice(1), parked_notice(2), parked_notice(3)]


def test_an_empty_parked_file_is_removed_without_a_note(
    greetings: list[Greeted], state: Path, tmp_path: Path
) -> None:
    path = park(state, tmp_path)
    assert runner.invoke(app, ["chat", "-C", str(tmp_path), "--no-verify"]).exit_code == 0

    assert not path.exists()
    [greeting] = greetings
    assert greeting.notices == [f"Workspace: {tmp_path.resolve()}"]


def test_a_parked_file_that_cant_be_read_stops_chat_naming_it(
    launched: list[AgentSession], state: Path, tmp_path: Path
) -> None:
    path = park(state, tmp_path, 1)
    path.write_text("not an envelope\n")
    result = runner.invoke(app, ["chat", "-C", str(tmp_path)])

    assert result.exit_code == 2
    assert f"Couldn't recover the events parked in {path}: Not JSON" in result.stderr
    assert launched == []
    assert store_threads() == []
    assert path.exists()


def test_a_database_newer_than_orbit_stops_chat(
    launched: list[AgentSession], state: Path, tmp_path: Path
) -> None:
    state.mkdir()
    with closing(sqlite3.connect(state / "orbit.db", isolation_level=None)) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version (version) VALUES (99)")
    result = runner.invoke(app, ["chat", "-C", str(tmp_path)])

    assert result.exit_code == 2
    assert "has schema version 99, newer than this orbit's 3" in result.output
    assert launched == []
    assert store_threads() == []


@contextmanager
def held(lock: Path) -> Iterator[None]:
    """Hold ``lock`` as another orbit would."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def test_old_sessions_another_orbit_has_open_stop_chat(
    launched: list[AgentSession], state: Path, tmp_path: Path
) -> None:
    sessions = legacy_sessions(tmp_path / ".orbit")
    with held(sessions / "abc.lock"):
        result = runner.invoke(app, ["chat", "-C", str(tmp_path)])

    assert result.exit_code == 2
    assert "an orbit still has a session there open" in result.output
    assert launched == []
    assert store_threads() == []
    assert (sessions / "abc.json").exists()
    assert asyncio.run(stored_ids(state, tmp_path)) == []


def test_two_orbits_with_one_state_directory_cant_open_one_session_at_once(
    launched: list[AgentSession], state: Path, tmp_path: Path
) -> None:
    taken: list[bool] = []

    async def open_twice(session: AgentSession) -> None:
        taken.append(await store_in(session).acquire("abc"))
        other = await SqliteStore.open(state, str(tmp_path.resolve()))
        try:
            taken.append(await other.acquire("abc"))
        finally:
            await other.close()

    FakeApp.during = open_twice
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0
    assert taken == [True, False]


def test_a_session_saved_opens_again_after_a_restart(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    loaded: list[SessionRecord | Unreadable] = []

    async def save(session: AgentSession) -> None:
        await store_in(session).save(SAVED)

    async def load(session: AgentSession) -> None:
        loaded.append(await store_in(session).load("abc"))

    for during in (save, load):
        FakeApp.during = during
        assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0
    assert loaded == [SAVED]


def test_a_missing_workspace_stops_chat(launched: list[AgentSession], tmp_path: Path) -> None:
    before = Path.cwd()
    result = runner.invoke(app, ["chat", "-C", str(tmp_path / "nowhere")])

    assert result.exit_code == 2
    assert "doesn't exist" in result.output
    assert Path.cwd() == before
    assert launched == []


def test_a_workspace_that_is_a_file_stops_chat(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("hi\n")
    result = runner.invoke(app, ["chat", "--workspace", str(file)])

    assert result.exit_code == 2
    assert "isn't a directory" in result.output
    assert launched == []


def test_chat_builds_the_backend_on_its_own_thread(launched: list[AgentSession]) -> None:
    assert runner.invoke(app, ["chat"]).exit_code == 0

    assert len(launched) == 1
    assert FakeApp.threads == ["orbit-backend"]


def test_chat_runs_tui_against_claude_cli(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None
    assert isinstance(session.agents.launcher, ClaudeCliRunner)  # The planner's too.
    assert session.settings == Settings()
    assert session.pinned == ()
    assert session.model_choices == MODEL_ALIASES


def test_chat_has_the_user_pick_a_session_at_startup(
    greetings: list[Greeted], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat"]).exit_code == 0

    [greeting] = greetings
    assert greeting.listed


def test_chat_new_starts_a_new_session_named_for_the_time(
    launched: list[AgentSession],
    greetings: list[Greeted],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat", "--new"]).exit_code == 0

    [session] = launched
    [greeting] = greetings
    assert not greeting.listed
    assert session.saved_session.name == default_name(session.saved_session.created)


def test_chat_name_starts_a_new_session_with_that_name(
    launched: list[AgentSession],
    greetings: list[Greeted],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat", "--name", "auth rework"]).exit_code == 0

    [session] = launched
    [greeting] = greetings
    assert not greeting.listed
    assert session.saved_session.name == "auth rework"


def test_chat_accepts_model_and_tool_access(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--model", "opus", "--tools", "full"])

    assert result.exit_code == 0
    [session] = launched
    assert session.settings == Settings(model="opus", tool_access="full")
    assert session.pinned == ("model", "tool_access")


def test_chat_pins_only_the_settings_given(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--tools", "off"])

    assert result.exit_code == 0
    [session] = launched
    assert session.settings == Settings(tool_access="off")
    assert session.pinned == ("tool_access",)


def test_chat_rejects_unknown_tool_access(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--tools", "sometimes"])

    assert result.exit_code == 2
    assert launched == []


def test_chat_verifies_tasks_with_the_project_files_steps(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text(
        '[[verification.steps]]\nname = "Lint"\ncommand = ["ruff", "check"]\n'
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    # The checks run as processes; tasks that change no files in the working tree skip them.
    steps = (VerificationStep(name="Lint", command=("ruff", "check")),)
    assert session.verification == Verification(steps, run_step, workspace_state)


def test_chat_without_steps_verifies_nothing(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    assert session.verification == Verification((), run_step, workspace_state)


def test_chat_can_turn_verification_off(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--no-verify"])

    assert result.exit_code == 0
    [session] = launched
    assert session.verification is None


def test_chat_runs_tasks_through_agents_connected_over_mcp(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    agents = session.agents
    assert agents is not None
    assert isinstance(agents.host, McpHost)
    assert isinstance(agents.launcher, ClaudeCliRunner)
    assert agents.limits == Limits(max_attempts=5)
    first, second = agents.new_token(), agents.new_token()
    assert first != second and len(first) >= 32


def test_chat_works_in_the_coder_tree_inside_a_git_repository(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orbit.cli.in_git_work_tree", lambda path: True)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None
    assert isinstance(session.agents.tree, GitCoderTree)


def test_chat_works_in_place_outside_a_git_repository(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orbit.cli.in_git_work_tree", lambda path: False)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None and session.agents.tree is None


def test_chat_can_turn_the_coder_tree_off(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--no-worktree"])
    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None and session.agents.tree is None


def test_chat_reads_roles_and_limits_from_the_project_file(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text(
        'fix_attempts = 2\n[roles.coder]\nmodel = "sonnet"\n'
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None
    assert session.agents.limits == Limits(max_attempts=2)
    assert session.agents.roles["coder"].model == "sonnet"


def test_chat_gives_verification_the_project_files_fallback_fixes(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text("fix_attempts = 2\nfallback_fixes = 7\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0
    [session] = launched
    assert session.verification is not None
    assert session.verification.fallback_fixes == 7
    assert session.agents is not None
    assert session.agents.limits == Limits(max_attempts=2)


def test_chat_sets_up_the_coder_tree_as_the_project_file_says(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text(
        '[setup]\ncommand = ["make", "deps"]\nwatch = ["deps.lock"]\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orbit.cli.in_git_work_tree", lambda path: True)
    runner.invoke(app, ["chat"])
    [session] = launched
    assert session.agents is not None
    assert isinstance(session.agents.tree, GitCoderTree)
    assert session.agents.tree.setup == Setup(command=("make", "deps"), watch=("deps.lock",))


def planner_role_read_after(config: Path, text: str, launched: list[AgentSession]) -> object:
    """The planner's role the chat's session reads again once ``config`` reads ``text``."""
    [session] = launched
    assert session.agents is not None and session.agents.planner_role is not None
    config.write_text(text)
    return session.agents.planner_role()


def test_chat_rereads_the_planners_role_from_the_project_file(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    config = tmp_path / ".orbit" / "config.toml"
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat"]).exit_code == 0
    role = planner_role_read_after(config, '[roles.planner]\nmodel = "opus"\n', launched)
    assert isinstance(role, Role) and role.model == "opus"


def test_rereading_an_invalid_project_file_gives_its_problems(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    config = tmp_path / ".orbit" / "config.toml"
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat"]).exit_code == 0
    error = planner_role_read_after(config, "[roles.planner]\ntools = ['fly']\n", launched)
    assert isinstance(error, ConfigError) and "roles.planner.tools" in str(error)


def test_an_invalid_project_file_stops_chat_with_its_problems(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text("[roles.coder]\ntool_access = 'maybe'\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert "roles.coder.tool_access" in result.output
    assert launched == []


def test_a_non_utf8_project_file_stops_chat_as_invalid(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_bytes(b"fix_attempts = '\xff'\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert "Invalid orbit configuration:" in result.output
    assert launched == []


def test_a_project_file_that_is_a_directory_stops_chat_as_invalid(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit" / "config.toml").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 2
    assert "Invalid orbit configuration:" in result.output
    assert launched == []


def test_chat_reviews_landed_work_with_the_files_limits(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text(
        "review_rounds = 2\nreview_suggestions = 4\nsuper_review_every = 7\n"
        "super_review_suggestions = 6\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orbit.cli.in_git_work_tree", lambda path: True)
    runner.invoke(app, ["chat"])
    [session] = launched
    agents = session.agents
    assert agents is not None
    assert agents.review
    assert agents.review_limits == ReviewLimits(
        rounds=2, suggestions=4, super_every=7, super_suggestions=6
    )


def test_chat_can_turn_review_off(launched: list[AgentSession]) -> None:
    runner.invoke(app, ["chat", "--no-review"])
    [session] = launched
    assert session.agents is not None and not session.agents.review


def test_chat_checks_the_steps_at_startup(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".orbit").mkdir()
    (tmp_path / ".orbit" / "config.toml").write_text(
        '[[verification.steps]]\nname = "Lint"\ncommand = ["ruff", "check"]\n'
    )
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat"]).exit_code == 0
    assert runner.invoke(app, ["chat", "--no-verify"]).exit_code == 0

    checked, unverified = launched
    assert startup_of(checked) is not None
    # Without verification, there's nothing to check.
    assert startup_of(unverified) is None


@pytest.fixture
def usage_sources(launched: list[AgentSession]) -> list[UsageSource | None]:
    """The usage sources chat gives its sessions."""
    return FakeApp.usage_sources


def test_chat_shows_usage_limits_from_the_claude_login(
    launched: list[AgentSession], usage_sources: list[UsageSource | None]
) -> None:
    assert runner.invoke(app, ["chat"]).exit_code == 0
    [source] = usage_sources
    assert isinstance(source, UsageApi)
    assert source.credentials == Path("~/.claude/.credentials.json").expanduser()


def test_chat_can_turn_usage_limits_off(
    launched: list[AgentSession], usage_sources: list[UsageSource | None]
) -> None:
    assert runner.invoke(app, ["chat", "--no-usage"]).exit_code == 0
    assert usage_sources == [None]
    assert len(launched) == 1


def test_chat_can_profile_the_app(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat", "-C", str(project), "--profile", "profile"])

    assert result.exit_code == 0
    assert len(launched) == 1
    # The directory is relative to where chat started, not the workspace. The app and the
    # backend ran while their loops were watched, and the profile, which sees both threads,
    # was written when the app ended.
    assert FakeApp.profiling == [["backend-blocked.log", "ui-blocked.log"]]
    assert sorted(p.name for p in FakeApp.profile_dir.iterdir()) == [
        "backend-blocked.log",
        "ui-blocked.log",
        "ui.prof",
    ]
    stats = pstats.Stats(str(tmp_path / "profile" / "ui.prof"))
    assert stats.total_calls > 0  # type: ignore[attr-defined]


def test_chat_runs_the_app_unprofiled_by_default(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat"]).exit_code == 0

    assert len(launched) == 1
    assert FakeApp.profiling == []
    assert not FakeApp.profile_dir.exists()


def test_exiting_while_halted_prints_why_to_stderr(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    FakeApp.halted = "Orbit stopped this session: saving the event log failed."
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert result.stderr == "Orbit stopped this session: saving the event log failed.\n"


def test_exiting_while_halted_after_profiling_prints_why_too(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    FakeApp.halted = "Halted."
    result = runner.invoke(app, ["chat", "--profile", "profile"])

    assert result.stderr == "Halted.\n"


def test_exiting_unhalted_prints_nothing(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["chat"]).stderr == ""


# Backups


async def saved_sessions(state: Path, workspace: Path, *ids: str) -> None:
    """Save sessions with ``ids`` for ``workspace`` in the store in ``state``."""
    store = await SqliteStore.open(state, str(workspace))
    try:
        for session_id in ids:
            await store.save(SessionRecord(id=session_id, name="x", created=WHEN, last_active=WHEN))
    finally:
        await store.close()


def backed_up_ids(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT id FROM sessions ORDER BY id").fetchall()
    return [str(session_id) for (session_id,) in rows]


def test_db_backup_copies_the_database_for_the_owner_alone(state: Path, tmp_path: Path) -> None:
    asyncio.run(saved_sessions(state, tmp_path, "first", "second"))
    result = runner.invoke(app, ["db", "backup", "out.db"])

    assert result.exit_code == 0
    target = Path("out.db").absolute()
    assert str(target) in result.stdout
    assert target.stat().st_mode & 0o777 == 0o600
    assert backed_up_ids(target) == ["first", "second"]


def test_db_backup_takes_the_state_directory_option(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    asyncio.run(saved_sessions(elsewhere, tmp_path, "abc"))
    result = runner.invoke(app, ["db", "backup", "out.db", "--state-dir", str(elsewhere)])

    assert result.exit_code == 0
    assert backed_up_ids(Path("out.db")) == ["abc"]


def test_db_backup_refuses_an_existing_file(state: Path, tmp_path: Path) -> None:
    asyncio.run(saved_sessions(state, tmp_path, "abc"))
    Path("out.db").write_text("mine")
    result = runner.invoke(app, ["db", "backup", "out.db"])

    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert "--force" in result.stderr
    assert Path("out.db").read_text() == "mine"


def test_db_backup_force_replaces_an_existing_file(state: Path, tmp_path: Path) -> None:
    asyncio.run(saved_sessions(state, tmp_path, "abc"))
    Path("out.db").write_text("mine")
    result = runner.invoke(app, ["db", "backup", "out.db", "--force"])

    assert result.exit_code == 0
    assert backed_up_ids(Path("out.db")) == ["abc"]


def test_db_backup_without_a_database_has_nothing_to_back_up(state: Path) -> None:
    result = runner.invoke(app, ["db", "backup", "out.db"])

    assert result.exit_code == 2
    assert "nothing to back up" in result.stderr
    assert not (state / "orbit.db").exists()
    assert not Path("out.db").exists()


def test_db_backup_that_fails_stops_with_the_reason(state: Path, tmp_path: Path) -> None:
    asyncio.run(saved_sessions(state, tmp_path, "abc"))
    result = runner.invoke(app, ["db", "backup", "missing/out.db"])

    assert result.exit_code == 2
    assert "Couldn't back up the database" in result.stderr
    assert store_threads() == []


def test_db_backup_while_another_orbit_has_the_database_open(state: Path, tmp_path: Path) -> None:
    async def backed_up_while_open() -> int:
        store = await SqliteStore.open(state, str(tmp_path))
        try:
            await store.save(SAVED)
            result = await asyncio.to_thread(runner.invoke, app, ["db", "backup", "out.db"])
            return result.exit_code
        finally:
            await store.close()

    assert asyncio.run(backed_up_while_open()) == 0
    assert backed_up_ids(Path("out.db")) == ["abc"]
