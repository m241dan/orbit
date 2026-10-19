"""Backend: the agentic loop. Must not depend on any frontend.

Layered, outermost first; each layer may import only the ones below it:

- ``external``: adapters that reach outside the backend (the Claude CLI, processes, git).
- ``application``: the session, which handles commands and drives the ports.
- ``domain``: the rules the application decides by. Pure.
"""

from orbit.agent.application.desk import Agents
from orbit.agent.application.handshake import NO_STEPS_NOTE
from orbit.agent.application.ports import (
    ConfigWriter,
    ProposalDecision,
    ProposalReview,
    SessionStore,
    StepRunner,
    StoredSession,
    Workspace,
)
from orbit.agent.application.recovery import RecoveryFailed, recover_parked
from orbit.agent.application.session import AgentSession
from orbit.agent.application.verifier import Verification
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.config import OrbitConfig
from orbit.agent.domain.config_errors import ConfigError
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.roles import Role
from orbit.agent.domain.server import ServerSettings, is_loopback
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.storage_halt import recovered_note
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.checks import run_step
from orbit.agent.external.claude_cli import MODEL_ALIASES, ClaudeCliRunner
from orbit.agent.external.config_file import read_config, write_checks
from orbit.agent.external.control_plane import ControlPlaneServer, ListenFailed
from orbit.agent.external.directories import resolve_config_dir, resolve_state_dir
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.mcp_host import McpHost
from orbit.agent.external.repository import git_top_level, in_git_work_tree, orbit_dir
from orbit.agent.external.run_file import RunFile, running_server
from orbit.agent.external.session_import import ImportBlocked, import_session_files, imported_note
from orbit.agent.external.sqlite_store import SqliteStore, StoreError
from orbit.agent.external.token_file import TokenError, TokenFile
from orbit.agent.external.usage_api import UsageApi
from orbit.agent.external.user_config import (
    USER_CONFIG_FILE,
    read_server,
    read_storage,
)
from orbit.agent.external.workspace import workspace_state
from orbit.agent.external.workspace_lock import WorkspaceLock

__all__ = [
    "MODEL_ALIASES",
    "NO_STEPS_NOTE",
    "USER_CONFIG_FILE",
    "AgentSession",
    "Agents",
    "ChecksProposal",
    "ClaudeCliRunner",
    "ConfigError",
    "ConfigWriter",
    "ControlPlaneServer",
    "FallbackFiles",
    "GitCoderTree",
    "ImportBlocked",
    "Limits",
    "ListenFailed",
    "McpHost",
    "OrbitConfig",
    "ProposalDecision",
    "ProposalReview",
    "RecoveryFailed",
    "Role",
    "RunFile",
    "ServerSettings",
    "SessionStore",
    "Setup",
    "SqliteStore",
    "StepRunner",
    "StorageSettings",
    "StoreError",
    "StoredSession",
    "TokenError",
    "TokenFile",
    "UsageApi",
    "Verification",
    "VerificationStep",
    "Workspace",
    "WorkspaceLock",
    "WorkspaceOwner",
    "git_top_level",
    "import_session_files",
    "imported_note",
    "in_git_work_tree",
    "is_loopback",
    "orbit_dir",
    "read_config",
    "read_server",
    "read_storage",
    "recover_parked",
    "recovered_note",
    "resolve_config_dir",
    "resolve_state_dir",
    "run_step",
    "running_server",
    "workspace_state",
    "write_checks",
]
