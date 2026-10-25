"""What ``orbit status`` reports of a running control plane, from what it answers, and how it's
printed: as text, or as one JSON object."""

from datetime import tzinfo

from pydantic import AwareDatetime, BaseModel, ConfigDict

from orbit.protocol import (
    AgentRef,
    AgentsChanged,
    Catalog,
    PauseReason,
    SessionState,
    SessionSummary,
    Usage,
    UsageTotals,
    Welcome,
    WorkspaceEntry,
)
from orbit.tui.view.sessions import ACTIVE_NOTE, paused_note, waiting_words
from orbit.tui.view.tasks import coder_wait


class _Report(BaseModel):
    model_config = ConfigDict(frozen=True)


class SessionStatus(_Report):
    """The session ``name``d, by its ``id``: whether it's its workspace's ``active`` one, its
    ``state``, whether its run is ``waiting_for_coder``, at ``coder_position`` in line if that's
    known, and, while its run is paused, when it starts again, ``paused_until``, why,
    ``paused_for``, and on which runtime, ``paused_runtime``."""

    id: str
    name: str
    active: bool
    state: SessionState
    waiting_for_coder: bool
    coder_position: int | None
    paused_until: AwareDatetime | None
    paused_for: PauseReason | None
    paused_runtime: str | None


class WorkspaceStatus(_Report):
    """The workspace ``id``, shown as ``label``, at ``path`` on the server's host if it has one,
    and its ``sessions``."""

    id: str
    label: str
    path: str | None
    sessions: tuple[SessionStatus, ...]


class AgentStatus(_Report):
    """Agent ``agent_id``, running in the ``role`` on task ``task_id``, in the session
    ``session`` of the workspace ``workspace``, shown as ``workspace_label``."""

    workspace: str
    workspace_label: str
    session: str
    agent_id: str
    role: str
    task_id: str


class Spend(_Report):
    """What turns cost: their ``tokens``, of every kind, and dollars, ``cost_usd``."""

    tokens: int
    cost_usd: float


class UsageStatus(_Report):
    """What turns cost in every workspace, ``today`` and over the ``lifetime``."""

    today: Spend
    lifetime: Spend


class StatusReport(_Report):
    """What the control plane at ``url``, orbit ``server_version``, is doing: the ``workspaces``
    it serves, the ``agents`` running, by workspace, role and task, and the ``usage``."""

    url: str
    server_version: str
    workspaces: tuple[WorkspaceStatus, ...]
    agents: tuple[AgentStatus, ...]
    usage: UsageStatus


def status_report(
    welcome: Welcome, catalog: Catalog, agents: AgentsChanged, totals: UsageTotals, url: str
) -> StatusReport:
    """What the control plane at ``url`` is doing, as its ``welcome``, ``catalog``, ``agents``
    and usage ``totals`` say."""
    labels = {entry.id: entry.label for entry in catalog.workspaces}
    running = sorted((_agent(ref, labels) for ref in agents.agents), key=_agent_order)
    return StatusReport(
        url=url,
        server_version=welcome.server_version,
        workspaces=tuple(_workspace(entry) for entry in catalog.workspaces),
        agents=tuple(running),
        usage=UsageStatus(today=_spend(totals.total.today), lifetime=_spend(totals.total.lifetime)),
    )


def _workspace(entry: WorkspaceEntry) -> WorkspaceStatus:
    """The workspace ``entry`` lists, with its sessions."""
    sessions = tuple(_session(summary) for summary in entry.sessions)
    return WorkspaceStatus(id=entry.id, label=entry.label, path=entry.path, sessions=sessions)


def _session(summary: SessionSummary) -> SessionStatus:
    """The session ``summary`` shows."""
    wait = coder_wait(summary.activity)
    return SessionStatus(
        id=summary.id,
        name=summary.name,
        active=summary.active,
        state=summary.state,
        waiting_for_coder=wait is not None,
        coder_position=None if wait is None else wait.position,
        paused_until=summary.paused_until,
        paused_for=summary.paused_for,
        paused_runtime=summary.paused_runtime,
    )


def _agent(ref: AgentRef, labels: dict[str, str]) -> AgentStatus:
    """The agent ``ref`` names, its workspace shown by its label in ``labels``, or by its id if
    it has none there."""
    return AgentStatus(
        workspace=ref.workspace,
        workspace_label=labels.get(ref.workspace, ref.workspace),
        session=ref.session,
        agent_id=ref.agent_id,
        role=ref.role,
        task_id=ref.task_id,
    )


def _agent_order(agent: AgentStatus) -> tuple[str, str, str]:
    """Where ``agent`` goes among those running: by workspace, role and task."""
    return agent.workspace_label, agent.role, agent.task_id


def _spend(usage: Usage) -> Spend:
    """The tokens and cost of ``usage``."""
    return Spend(tokens=usage.tokens, cost_usd=usage.cost_usd)


def status_json(report: StatusReport) -> str:
    """``report`` as one JSON object."""
    return report.model_dump_json(indent=2)


def status_text(report: StatusReport, zone: tzinfo | None = None) -> str:
    """``report`` as text: the server, then its workspaces with their sessions, the agents
    running and the usage, with times in ``zone`` (the local one by default)."""
    workspaces = [line for each in report.workspaces for line in _workspace_lines(each, zone)]
    agents = [f"  {_agent_line(agent)}" for agent in report.agents]
    sections = [
        [f"orbit serve {report.server_version} at {report.url}"],
        _section("Workspaces", workspaces),
        _section("Agents", agents),
        _section("Usage", _usage_lines(report.usage)),
    ]
    return "\n\n".join("\n".join(lines) for lines in sections)


def _section(title: str, lines: list[str]) -> list[str]:
    """The section ``title``d holding ``lines``, or saying there's none."""
    return [title, *(lines or ["  none"])]


def _workspace_lines(workspace: WorkspaceStatus, zone: tzinfo | None) -> list[str]:
    """``workspace``, by its label and path, then its sessions, with times in ``zone``."""
    heading = workspace.label if workspace.path is None else f"{workspace.label}  {workspace.path}"
    sessions = [f"    {_session_line(session, zone)}" for session in workspace.sessions]
    return [f"  {heading}", *(sessions or ["    no sessions"])]


def _session_line(session: SessionStatus, zone: tzinfo | None) -> str:
    """``session``: its name, whether it's active, its state or its wait for a coder, and,
    while its run is paused, until when, a time in ``zone``: ``main · active · running``."""
    notes = [session.name, *([ACTIVE_NOTE] if session.active else [])]
    notes.append(
        waiting_words(session.coder_position) if session.waiting_for_coder else session.state
    )
    if session.paused_until is not None:
        paused = paused_note(session.paused_until, session.paused_for, session.paused_runtime, zone)
        notes.append(paused)
    return " · ".join(notes)


def _agent_line(agent: AgentStatus) -> str:
    """``agent``, by its workspace, role and task: ``orbit · coder · task 3``."""
    return f"{agent.workspace_label} · {agent.role} · task {agent.task_id}"


def _usage_lines(usage: UsageStatus) -> list[str]:
    """What turns cost today and over the lifetime: ``today     1,200 tokens · $0.25``."""
    return [f"  today     {_spend_text(usage.today)}", f"  lifetime  {_spend_text(usage.lifetime)}"]


def _spend_text(spend: Spend) -> str:
    """``spend``'s tokens and cost: ``1,200 tokens · $0.25``."""
    return f"{spend.tokens:,} tokens · ${spend.cost_usd:.2f}"
