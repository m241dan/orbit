"""Backend: the agentic loop. Must not depend on any frontend.

Layered, outermost first; each layer may import only the ones below it:

- ``external``: adapters that reach outside the backend (the Claude CLI, processes, git).
- ``application``: the control plane and the workspace hosts, which handle commands and drive
  the ports.
- ``domain``: the rules the application decides by. Pure.
"""

from orbit.agent.application.control_plane import (
    ControlPlane,
    WorkspaceSource,
    WorkspaceUnavailable,
)
from orbit.agent.application.desk import Agents
from orbit.agent.application.handshake import NO_STEPS_NOTE
from orbit.agent.application.ports import (
    ConfigWriter,
    ProposalDecision,
    ProposalReview,
    SessionStore,
    StepRunner,
    StoredSession,
    StoreFailed,
    Workspace,
)
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.application.verifier import Verification
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.config import OrbitConfig
from orbit.agent.domain.config_errors import INVALID_CONFIG, ConfigError
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.remotes import RemoteSettings
from orbit.agent.domain.roles import Role
from orbit.agent.domain.server import LocalTls, RunningServer, ServerSettings, TlsMode
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.domain.workspaces import PathKind
from orbit.agent.external.checks import run_step
from orbit.agent.external.claude_cli import MODEL_ALIASES, ClaudeCliRunner
from orbit.agent.external.config_file import read_config, write_checks
from orbit.agent.external.control_plane import ControlPlaneServer, ListenFailed
from orbit.agent.external.directories import resolve_config_dir, resolve_state_dir
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.mcp_host import McpHost
from orbit.agent.external.repository import (
    git_top_level,
    has_git_identity,
    in_git_work_tree,
    orbit_dir,
)
from orbit.agent.external.run_file import RunFile, ServeLock, running_server
from orbit.agent.external.served_workspaces import (
    HostMaker,
    ServedWorkspace,
    ServedWorkspaces,
    locked,
    top_level,
)
from orbit.agent.external.session_import import ImportBlocked
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore, WorkspaceStore
from orbit.agent.external.store_startup import OpenedStore, open_workspace_store
from orbit.agent.external.tls import LocalDiscoveryUnavailable, TlsError, local_tls, tls_warnings
from orbit.agent.external.token_file import TokenError, TokenFile
from orbit.agent.external.usage_api import UsageApi
from orbit.agent.external.user_config import (
    USER_CONFIG_FILE,
    read_remotes,
    read_server,
    read_storage,
)
from orbit.agent.external.workspace import workspace_state
from orbit.agent.external.workspace_lock import WorkspaceLock
from orbit.agent.external.workspace_registry import WorkspaceRegistry

__all__ = [
    "INVALID_CONFIG",
    "MODEL_ALIASES",
    "NO_STEPS_NOTE",
    "USER_CONFIG_FILE",
    "Agents",
    "ChecksProposal",
    "ClaudeCliRunner",
    "ConfigError",
    "ConfigWriter",
    "ControlPlane",
    "ControlPlaneServer",
    "FallbackFiles",
    "GitCoderTree",
    "HostMaker",
    "ImportBlocked",
    "Limits",
    "ListenFailed",
    "LocalDiscoveryUnavailable",
    "LocalTls",
    "McpHost",
    "OpenedStore",
    "OrbitConfig",
    "PathKind",
    "ProposalDecision",
    "ProposalReview",
    "RecoveryFailed",
    "RemoteSettings",
    "Role",
    "RunFile",
    "RunningServer",
    "ServeLock",
    "ServedWorkspace",
    "ServedWorkspaces",
    "ServerSettings",
    "SessionStore",
    "Setup",
    "SqliteStore",
    "StepRunner",
    "StorageSettings",
    "StoreError",
    "StoreFailed",
    "StoredSession",
    "TlsError",
    "TlsMode",
    "TokenError",
    "TokenFile",
    "UsageApi",
    "Verification",
    "VerificationStep",
    "Workspace",
    "WorkspaceHost",
    "WorkspaceLock",
    "WorkspaceOwner",
    "WorkspaceRegistry",
    "WorkspaceSource",
    "WorkspaceStore",
    "WorkspaceUnavailable",
    "git_top_level",
    "has_git_identity",
    "in_git_work_tree",
    "local_tls",
    "locked",
    "open_workspace_store",
    "orbit_dir",
    "read_config",
    "read_remotes",
    "read_server",
    "read_storage",
    "resolve_config_dir",
    "resolve_state_dir",
    "run_step",
    "running_server",
    "tls_warnings",
    "top_level",
    "workspace_state",
    "write_checks",
]
