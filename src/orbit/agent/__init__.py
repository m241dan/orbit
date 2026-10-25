"""Backend: the agentic loop. Must not depend on any frontend.

Layered, outermost first; each layer may import only the ones below it:

- ``external``: adapters that reach outside the backend (the Claude CLI, processes, git).
- ``application``: the control plane and the workspace hosts, which handle commands and drive
  the ports.
- ``domain``: the rules the application decides by. Pure.
"""

from orbit.agent.application.browsing import Browsing
from orbit.agent.application.control_plane import ControlPlane
from orbit.agent.application.desk import Agents
from orbit.agent.application.handshake import NO_STEPS_NOTE
from orbit.agent.application.ports import (
    AgentLauncher,
    CloneFailed,
    ConfigWriter,
    ProposalDecision,
    ProposalReview,
    SessionStore,
    StepRunner,
    StoredSession,
    StoreFailed,
    Workspace,
)
from orbit.agent.application.purge import purge
from orbit.agent.application.recovery import RecoveryFailed
from orbit.agent.application.verifier import Verification
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.application.workspace_source import WorkspaceSource, WorkspaceUnavailable
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.certificate_list import certificate_lines
from orbit.agent.domain.checks import ChecksProposal, Setup
from orbit.agent.domain.client_bundle import (
    REMOTE_FILE,
    bundle_directory,
    bundle_url,
    client_bundle,
    is_bundle_url,
)
from orbit.agent.domain.config import LIMIT_KEYS, OrbitConfig
from orbit.agent.domain.config_errors import INVALID_CONFIG, ConfigError
from orbit.agent.domain.config_report import (
    ConfigReport,
    Instructions,
    Setting,
    UnknownKey,
    config_report,
    subtree,
)
from orbit.agent.domain.config_update import InstructionLines, instructions_setting
from orbit.agent.domain.layers import Layer
from orbit.agent.domain.ownership import WorkspaceOwner
from orbit.agent.domain.purging import Purged, PurgeRefused
from orbit.agent.domain.remotes import RemoteSettings
from orbit.agent.domain.roles import Role, runtime_roles
from orbit.agent.domain.runtimes import CLAUDE, known_runtimes, knows_runtime, not_on_path
from orbit.agent.domain.server import LocalTls, RunningServer, ServerSettings, TlsMode
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.domain.workspaces import PathKind
from orbit.agent.external.certificates import (
    CA_DAYS,
    CERTIFICATE_DAYS,
    CertificateError,
    CertsDir,
    KeyType,
    PrivateKey,
    certificate_pem,
    fingerprint,
    issue_client,
    issue_server,
    key_pem,
    make_ca,
    read_certificate,
    server_names,
    write_bundle,
)
from orbit.agent.external.checks import run_step
from orbit.agent.external.claude_cli import ClaudeCliRunner
from orbit.agent.external.cloner import Cloner
from orbit.agent.external.config_file import read_config, read_config_sources, write_checks
from orbit.agent.external.config_init import (
    ConfigExists,
    InitResult,
    RolesResult,
    UpdateResult,
    add_roles,
    init_config,
    update_config,
)
from orbit.agent.external.control_plane import ControlPlaneServer, ListenFailed
from orbit.agent.external.directories import resolve_config_dir, resolve_state_dir
from orbit.agent.external.directory_lister import DirectoryLister
from orbit.agent.external.fallback_files import FallbackFiles
from orbit.agent.external.file_names import file_names
from orbit.agent.external.git_tree import GitCoderTree
from orbit.agent.external.mcp_host import McpHost
from orbit.agent.external.repository import (
    RepositoryUnreadable,
    git_top_level,
    has_git_identity,
    in_git_work_tree,
    orbit_dir,
)
from orbit.agent.external.run_file import RunFile, ServeLock, running_server
from orbit.agent.external.runtimes import read_package_models
from orbit.agent.external.served_workspaces import (
    Configuring,
    HostMaker,
    OpenedWorkspace,
    Opening,
    ServedWorkspace,
    ServedWorkspaces,
    open_workspace,
    top_level,
)
from orbit.agent.external.session_import import ImportBlocked
from orbit.agent.external.sqlite_schema import StoreError
from orbit.agent.external.sqlite_store import SqliteStore, WorkspaceStore
from orbit.agent.external.store_startup import OpenedStore, open_workspace_store
from orbit.agent.external.temp_dirs import SystemTempDirs
from orbit.agent.external.tls import LocalDiscoveryUnavailable, TlsError, local_tls, tls_warnings
from orbit.agent.external.token_file import TokenError, TokenFile
from orbit.agent.external.usage_api import UsageApi
from orbit.agent.external.user_config import (
    USER_CONFIG_FILE,
    read_remotes,
    read_server,
    read_storage,
    read_user_config,
)
from orbit.agent.external.workspace import workspace_state
from orbit.agent.external.workspace_lock import WorkspaceLock
from orbit.agent.external.workspace_purge import StorePurging, forget_workspace, purge_workspace
from orbit.agent.external.workspace_registry import WorkspaceRegistry

__all__ = [
    "CA_DAYS",
    "CERTIFICATE_DAYS",
    "CLAUDE",
    "INVALID_CONFIG",
    "LIMIT_KEYS",
    "NO_STEPS_NOTE",
    "REMOTE_FILE",
    "USER_CONFIG_FILE",
    "AgentLauncher",
    "Agents",
    "Browsing",
    "CertificateError",
    "CertsDir",
    "ChecksProposal",
    "ClaudeCliRunner",
    "CloneFailed",
    "Cloner",
    "ConfigError",
    "ConfigExists",
    "ConfigReport",
    "ConfigWriter",
    "Configuring",
    "ControlPlane",
    "ControlPlaneServer",
    "DirectoryLister",
    "FallbackFiles",
    "GitCoderTree",
    "HostMaker",
    "ImportBlocked",
    "InitResult",
    "InstructionLines",
    "Instructions",
    "KeyType",
    "Layer",
    "Limits",
    "ListenFailed",
    "LocalDiscoveryUnavailable",
    "LocalTls",
    "McpHost",
    "OpenedStore",
    "OpenedWorkspace",
    "Opening",
    "OrbitConfig",
    "PathKind",
    "PrivateKey",
    "ProposalDecision",
    "ProposalReview",
    "PurgeRefused",
    "Purged",
    "RecoveryFailed",
    "RemoteSettings",
    "RepositoryUnreadable",
    "Role",
    "RolesResult",
    "RunFile",
    "RunningServer",
    "ServeLock",
    "ServedWorkspace",
    "ServedWorkspaces",
    "ServerSettings",
    "SessionStore",
    "Setting",
    "Setup",
    "SqliteStore",
    "StepRunner",
    "StorageSettings",
    "StoreError",
    "StoreFailed",
    "StorePurging",
    "StoredSession",
    "SystemTempDirs",
    "TlsError",
    "TlsMode",
    "TokenError",
    "TokenFile",
    "UnknownKey",
    "UpdateResult",
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
    "add_roles",
    "bundle_directory",
    "bundle_url",
    "certificate_lines",
    "certificate_pem",
    "client_bundle",
    "config_report",
    "file_names",
    "fingerprint",
    "forget_workspace",
    "git_top_level",
    "has_git_identity",
    "in_git_work_tree",
    "init_config",
    "instructions_setting",
    "is_bundle_url",
    "issue_client",
    "issue_server",
    "key_pem",
    "known_runtimes",
    "knows_runtime",
    "local_tls",
    "make_ca",
    "not_on_path",
    "open_workspace",
    "open_workspace_store",
    "orbit_dir",
    "purge",
    "purge_workspace",
    "read_certificate",
    "read_config",
    "read_config_sources",
    "read_package_models",
    "read_remotes",
    "read_server",
    "read_storage",
    "read_user_config",
    "resolve_config_dir",
    "resolve_state_dir",
    "run_step",
    "running_server",
    "runtime_roles",
    "server_names",
    "subtree",
    "tls_warnings",
    "top_level",
    "update_config",
    "workspace_state",
    "write_bundle",
    "write_checks",
]
