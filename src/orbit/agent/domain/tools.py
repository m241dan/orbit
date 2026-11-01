"""Orbit's tools: the names of those its MCP server offers agents, and which of them an agent may
call, granted or withheld, as each runtime is told."""

from collections.abc import Sequence
from dataclasses import dataclass

GET_TASK = "get_task"
COMPLETE_TASK = "complete_task"
RUN_CHECK = "run_check"
REPORT_PROGRESS = "report_progress"
GIVE_UP = "give_up"
SUGGEST_TASK = "suggest_task"
PROPOSE_TASK = "propose_task"
PROPOSE_CHECKS = "propose_checks"
CONSULT = "consult"
SAVE_REPORT = "save_report"
LIST_REPORTS = "list_reports"
READ_REPORT = "read_report"
LIST_DRAFTS = "list_drafts"
READ_DRAFT = "read_draft"
EDIT_DRAFT = "edit_draft"
REMOVE_DRAFT = "remove_draft"
MOVE_DRAFT = "move_draft"
READ_SKILL = "read_skill"

DRAFT_TOOLS = (LIST_DRAFTS, READ_DRAFT, EDIT_DRAFT, REMOVE_DRAFT, MOVE_DRAFT)
"""The tools a role that proposes tasks is granted for its drafts."""

ORBIT_TOOLS = (
    GET_TASK,
    COMPLETE_TASK,
    RUN_CHECK,
    REPORT_PROGRESS,
    GIVE_UP,
    SUGGEST_TASK,
    PROPOSE_TASK,
    PROPOSE_CHECKS,
    CONSULT,
    SAVE_REPORT,
    LIST_REPORTS,
    READ_REPORT,
    *DRAFT_TOOLS,
    READ_SKILL,
)
"""Every tool orbit's MCP server offers agents."""


@dataclass(frozen=True)
class OrbitToolGrant:
    """Orbit's tools an agent may call, and its others, which it may not, by orbit's names for
    them; each runtime names them its own way."""

    granted: tuple[str, ...]
    withheld: tuple[str, ...]


def orbit_tool_grant(tools: Sequence[str]) -> OrbitToolGrant:
    """Which of orbit's tools an agent granted ``tools`` may call: those, in their order, and
    none of orbit's others, in orbit's."""
    return OrbitToolGrant(tuple(tools), tuple(tool for tool in ORBIT_TOOLS if tool not in tools))
