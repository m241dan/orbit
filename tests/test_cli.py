import subprocess
from pathlib import Path
from typing import ClassVar

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
    Setup,
    Verification,
    VerificationStep,
    run_step,
    workspace_state,
)
from orbit.agent.domain.reviews import ReviewLimits
from orbit.cli import app
from orbit.protocol import Notice, Settings

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


class FakeApp:
    launched: ClassVar[list[AgentSession]] = []

    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def run(self) -> None:
        FakeApp.launched.append(self.session)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[AgentSession]:
    monkeypatch.setattr("orbit.cli.OrbitApp", FakeApp)
    FakeApp.launched = []
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


def test_chat_runs_tui_against_claude_cli(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 0
    [session] = launched
    assert session.agents is not None
    assert isinstance(session.agents.launcher, ClaudeCliRunner)  # The planner's too.
    assert session.settings == Settings()
    assert session.model_choices == MODEL_ALIASES


def test_chat_accepts_model_and_tool_access(launched: list[AgentSession]) -> None:
    result = runner.invoke(app, ["chat", "--model", "opus", "--tools", "full"])

    assert result.exit_code == 0
    [session] = launched
    assert session.settings == Settings(model="opus", tool_access="full")


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
