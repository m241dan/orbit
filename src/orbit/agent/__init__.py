"""Backend: the agentic loop. Must not depend on any frontend.

Layered, outermost first; each layer may import only the ones below it:

- ``external``: adapters that reach outside the backend (the Claude CLI, processes, git).
- ``application``: the session, which handles commands and drives the ports.
- ``domain``: the rules the application decides by. Pure.
"""

from orbit.agent.application.desk import Agents
from orbit.agent.application.ports import (
    ConfigWriter,
    ProposalDecision,
    ProposalReview,
    SessionStore,
    StepRunner,
    StoredSession,
    Workspace,
)
from orbit.agent.application.session import NO_STEPS_NOTE, AgentSession
from orbit.agent.application.verifier import Verification
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.roles import ChecksProposal, ConfigError, OrbitConfig, Role, Setup
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.checks import run_step
from orbit.agent.external.claude_cli import MODEL_ALIASES, ClaudeCliRunner
from orbit.agent.external.config_file import read_config, write_checks
from orbit.agent.external.git_tree import GitCoderTree, git_top_level, in_git_work_tree
from orbit.agent.external.mcp_host import McpHost
from orbit.agent.external.session_files import SessionFiles, state_dir
from orbit.agent.external.usage_api import UsageApi
from orbit.agent.external.workspace import workspace_state

__all__ = [
    "MODEL_ALIASES",
    "NO_STEPS_NOTE",
    "AgentSession",
    "Agents",
    "ChecksProposal",
    "ClaudeCliRunner",
    "ConfigError",
    "ConfigWriter",
    "GitCoderTree",
    "Limits",
    "McpHost",
    "OrbitConfig",
    "ProposalDecision",
    "ProposalReview",
    "Role",
    "SessionFiles",
    "SessionStore",
    "Setup",
    "StepRunner",
    "StoredSession",
    "UsageApi",
    "Verification",
    "VerificationStep",
    "Workspace",
    "git_top_level",
    "in_git_work_tree",
    "read_config",
    "run_step",
    "state_dir",
    "workspace_state",
    "write_checks",
]
