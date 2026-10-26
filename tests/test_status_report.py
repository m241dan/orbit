"""What ``orbit status`` reports of a running control plane, as text and as one JSON object."""

import json
from datetime import UTC, datetime

from orbit.cli.status_report import status_json, status_report, status_text
from orbit.protocol import (
    PAUSED_FOR_USAGE_LIMIT,
    Activity,
    AgentRef,
    AgentsChanged,
    Catalog,
    SessionSummary,
    Usage,
    UsageTotal,
    UsageTotals,
    Welcome,
    WorkspaceEntry,
)

URL = "ws://127.0.0.1:7433/"
WELCOME = Welcome(command_id="hello", server_version="0.1.0")
UNTIL = datetime(2026, 10, 25, 18, 1, tzinfo=UTC)

MAIN = SessionSummary(id="s1", name="main", active=True, state="running")
WAITING = SessionSummary(
    id="s2",
    name="docs",
    state="running",
    activity=(
        Activity(kind="task_run", task_id="t2"),
        Activity(kind="waiting_for_coder", position=2, busy=4),
    ),
)
PAUSED = SessionSummary(
    id="s3",
    name="old",
    paused_until=UNTIL,
    paused_for=PAUSED_FOR_USAGE_LIMIT,
    paused_runtime="claude",
)

CATALOG = Catalog(
    workspaces=(
        WorkspaceEntry(id="w1", label="orbit", path="/src/orbit", sessions=(MAIN, WAITING, PAUSED)),
        WorkspaceEntry(id="w2", label="scratch", sessions=()),
    ),
    agents=(),
    triggers=(),
    lanes=(),
)
AGENTS = AgentsChanged(
    agents=(
        AgentRef(workspace="w1", session="s1", agent_id="agent-4", role="reviewer", task_id="t0"),
        AgentRef(workspace="w1", session="s1", agent_id="agent-3", role="coder", task_id="t1"),
    )
)
TODAY = Usage(input_tokens=1000, output_tokens=200, cost_usd=0.25)
LIFETIME = Usage(input_tokens=5000, cache_read_input_tokens=1000, cost_usd=1.5)
TOTALS = UsageTotals(workspaces=(), total=UsageTotal(today=TODAY, lifetime=LIFETIME))

NOTHING = Catalog(workspaces=(), agents=(), triggers=(), lanes=())
NO_AGENTS = AgentsChanged(agents=())
NO_TOTALS = UsageTotals(workspaces=(), total=UsageTotal(today=Usage(), lifetime=Usage()))

TEXT = """\
orbit serve 0.1.0 at ws://127.0.0.1:7433/

Workspaces
  orbit  /src/orbit
    main · active · running
    docs · waiting for a coder: 2nd in line
    old · idle · run paused: usage limit on claude; starts again at 18:01
  scratch
    no sessions

Agents
  orbit · coder · task t1
  orbit · reviewer · task t0

Usage
  today     1,200 tokens · $0.25
  lifetime  6,000 tokens · $1.50"""


def test_the_text_gives_the_server_its_workspaces_sessions_agents_and_usage() -> None:
    report = status_report(WELCOME, CATALOG, AGENTS, TOTALS, URL)
    assert status_text(report, UTC) == TEXT


def test_a_session_waiting_for_a_coder_is_reported_with_its_place_in_line() -> None:
    (workspace, _) = status_report(WELCOME, CATALOG, AGENTS, TOTALS, URL).workspaces
    docs = workspace.sessions[1]
    assert (docs.state, docs.waiting_for_coder, docs.coder_position) == ("running", True, 2)
    assert (docs.paused_until, docs.paused_for, docs.paused_runtime) == (None, None, None)


def test_a_paused_session_is_reported_with_when_why_and_on_which_runtime() -> None:
    (workspace, _) = status_report(WELCOME, CATALOG, AGENTS, TOTALS, URL).workspaces
    old = workspace.sessions[2]
    assert (old.state, old.waiting_for_coder, old.coder_position) == ("idle", False, None)
    assert (old.paused_until, old.paused_for, old.paused_runtime) == (
        UNTIL,
        PAUSED_FOR_USAGE_LIMIT,
        "claude",
    )


def test_a_wait_for_a_coder_with_no_place_in_line_says_just_that() -> None:
    waiting = WAITING.model_copy(update={"activity": (Activity(kind="waiting_for_coder"),)})
    entry = WorkspaceEntry(id="w1", label="orbit", sessions=(waiting,))
    catalog = NOTHING.model_copy(update={"workspaces": (entry,)})
    text = status_text(status_report(WELCOME, catalog, NO_AGENTS, NO_TOTALS, URL))
    assert "    docs · waiting for a coder\n" in text


def test_agents_are_ordered_by_workspace_role_and_task() -> None:
    agents = AgentsChanged(
        agents=(
            AgentRef(workspace="w2", session="s9", agent_id="agent-9", role="coder", task_id="a"),
            AgentRef(workspace="w1", session="s1", agent_id="agent-2", role="coder", task_id="t2"),
            AgentRef(workspace="w1", session="s1", agent_id="agent-1", role="coder", task_id="t1"),
        )
    )
    report = status_report(WELCOME, CATALOG, agents, TOTALS, URL)
    assert [agent.agent_id for agent in report.agents] == ["agent-1", "agent-2", "agent-9"]
    assert [agent.workspace_label for agent in report.agents] == ["orbit", "orbit", "scratch"]


def test_an_agent_of_a_workspace_the_catalog_doesnt_list_goes_by_its_id() -> None:
    agents = AgentsChanged(
        agents=(AgentRef(workspace="w7", session="s", agent_id="a", role="coder", task_id="t"),)
    )
    report = status_report(WELCOME, NOTHING, agents, NO_TOTALS, URL)
    assert "  w7 · coder · task t\n" in status_text(report)


def test_nothing_served_running_or_spent_says_so() -> None:
    report = status_report(WELCOME, NOTHING, NO_AGENTS, NO_TOTALS, URL)
    assert status_text(report) == (
        "orbit serve 0.1.0 at ws://127.0.0.1:7433/\n\n"
        "Workspaces\n  none\n\n"
        "Agents\n  none\n\n"
        "Usage\n  today     0 tokens · $0.00\n  lifetime  0 tokens · $0.00"
    )


def test_the_json_is_one_object_with_the_same_values() -> None:
    report = status_report(WELCOME, CATALOG, AGENTS, TOTALS, URL)
    parsed = json.loads(status_json(report))
    assert (parsed["url"], parsed["server_version"]) == (URL, "0.1.0")
    orbit, scratch = parsed["workspaces"]
    assert (orbit["id"], orbit["label"], orbit["path"]) == ("w1", "orbit", "/src/orbit")
    assert (scratch["path"], scratch["sessions"]) == (None, [])
    assert orbit["sessions"][0] == {
        "id": "s1",
        "name": "main",
        "active": True,
        "state": "running",
        "waiting_for_coder": False,
        "coder_position": None,
        "paused_until": None,
        "paused_for": None,
        "paused_runtime": None,
    }
    assert orbit["sessions"][1]["coder_position"] == 2
    assert orbit["sessions"][2]["paused_until"] == "2026-10-25T18:01:00Z"
    assert orbit["sessions"][2]["paused_for"] == "usage_limit"
    assert orbit["sessions"][2]["paused_runtime"] == "claude"
    assert parsed["agents"][0] == {
        "workspace": "w1",
        "workspace_label": "orbit",
        "session": "s1",
        "agent_id": "agent-3",
        "role": "coder",
        "task_id": "t1",
    }
    assert parsed["usage"] == {
        "today": {"tokens": 1200, "cost_usd": 0.25, "credits": 0.0},
        "lifetime": {"tokens": 6000, "cost_usd": 1.5, "credits": 0.0},
    }


def test_credits_are_reported_beside_dollars() -> None:
    lifetime = LIFETIME + Usage(credits=0.62)
    totals = UsageTotals(workspaces=(), total=UsageTotal(today=Usage(credits=2), lifetime=lifetime))
    report = status_report(WELCOME, NOTHING, NO_AGENTS, totals, URL)
    assert status_text(report).endswith(
        "Usage\n  today     2.00 credits\n  lifetime  6,000 tokens · $1.50 · 0.62 credits"
    )
    assert json.loads(status_json(report))["usage"] == {
        "today": {"tokens": 0, "cost_usd": 0.0, "credits": 2.0},
        "lifetime": {"tokens": 6000, "cost_usd": 1.5, "credits": 0.62},
    }
