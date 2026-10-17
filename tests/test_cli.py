import asyncio
import pstats
import subprocess
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from orbit import __version__
from orbit.agent import (
    MODEL_ALIASES,
    AgentSession,
    ClaudeCliRunner,
    GitCoderTree,
    Limits,
    McpHost,
    SessionFiles,
    Setup,
    UsageApi,
    Verification,
    VerificationStep,
    run_step,
    workspace_state,
)
from orbit.agent.application.ports import UsageSource
from orbit.agent.domain.reviews import ReviewLimits
from orbit.cli import app
from orbit.protocol import Notice, Settings, ThreadedSession

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

    def __init__(self, session: ThreadedSession) -> None:
        self.session = session

    def run(self) -> None:
        asyncio.run(self._open_and_close())

    async def run_async(self) -> None:
        await self._open_and_close()

    async def _open_and_close(self) -> None:
        self.session.start()
        if FakeApp.profile_dir.exists():
            FakeApp.profiling.append(sorted(p.name for p in FakeApp.profile_dir.iterdir()))
        await self.session.close()


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
    return FakeApp.launched


@pytest.fixture(autouse=True)
def restore_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat changes into its workspace; put the working directory back afterwards."""
    monkeypatch.chdir(Path.cwd())


def notices(session: AgentSession) -> list[str]:
    events = []
    while not session._events.empty():
        events.append(session._events.get_nowait())
    return [e.message for e in events if isinstance(e, Notice)]


def test_chat_works_in_the_current_directory_by_default(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    assert Path.cwd() == tmp_path.resolve()
    [session] = launched
    assert f"Workspace: {tmp_path.resolve()}" in notices(session)


@pytest.mark.parametrize("flag", ["-C", "--workspace"])
def test_chat_works_in_the_workspace_it_is_given(
    launched: list[AgentSession], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str
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
    # The project's own configuration is read, from the workspace.
    assert session.agents is not None
    assert session.agents.limits == Limits(max_attempts=3, max_checks=4)
    assert f"Workspace: {project.resolve()}" in notices(session)


def test_a_workspace_inside_a_git_repository_is_its_top_level(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    result = runner.invoke(app, ["chat", "-C", str(sub)])

    assert result.exit_code == 0
    assert Path.cwd() == repo.resolve()
    [session] = launched
    assert session.agents is not None
    assert isinstance(session.agents.tree, GitCoderTree)
    assert f"Workspace: {repo.resolve()}" in notices(session)


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


def test_chat_saves_sessions_in_the_git_directory_inside_a_repository(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert runner.invoke(app, ["chat", "-C", str(repo)]).exit_code == 0

    [session] = launched
    store = session.saved_session._store
    assert isinstance(store, SessionFiles)
    assert store._sessions == repo.resolve() / ".git" / "orbit" / "sessions"


def test_chat_saves_sessions_in_the_workspace_outside_git(
    launched: list[AgentSession], tmp_path: Path
) -> None:
    assert runner.invoke(app, ["chat", "-C", str(tmp_path)]).exit_code == 0

    [session] = launched
    store = session.saved_session._store
    assert isinstance(store, SessionFiles)
    assert store._sessions == tmp_path.resolve() / ".orbit" / "sessions"
    assert not (tmp_path / ".orbit").exists()  # Nothing was used, so nothing was saved.


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
    assert checked._startup is not None
    # Without verification, there's nothing to check.
    assert unverified._startup is None


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
