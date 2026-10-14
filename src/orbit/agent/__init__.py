"""Backend: the agentic loop. Must not depend on any frontend.

Layered, outermost first; each layer may import only the ones below it:

- ``external``: adapters that reach outside the backend (the Claude CLI, processes, git).
- ``application``: the session, which handles commands and drives the ports.
- ``domain``: the rules the application decides by. Pure.
"""

from orbit.agent.application.agent_run import Agents
from orbit.agent.application.loop import ModelLoopRunner
from orbit.agent.application.ports import ModelClient, StepRunner, Tool, TurnRunner, Workspace
from orbit.agent.application.session import AgentSession
from orbit.agent.application.tools import ToolRegistry
from orbit.agent.application.verifier import Verification
from orbit.agent.domain.agent_task import Limits
from orbit.agent.domain.verification import VerificationStep
from orbit.agent.external.checks import DEFAULT_STEPS, run_step
from orbit.agent.external.claude_cli import MODEL_ALIASES, ClaudeCliRunner
from orbit.agent.external.echo import EchoModel
from orbit.agent.external.git_tree import GitCoderTree, in_git_work_tree
from orbit.agent.external.mcp_host import McpHost
from orbit.agent.external.workspace import workspace_state

__all__ = [
    "DEFAULT_STEPS",
    "MODEL_ALIASES",
    "AgentSession",
    "Agents",
    "ClaudeCliRunner",
    "EchoModel",
    "GitCoderTree",
    "Limits",
    "McpHost",
    "ModelClient",
    "ModelLoopRunner",
    "StepRunner",
    "Tool",
    "ToolRegistry",
    "TurnRunner",
    "Verification",
    "VerificationStep",
    "Workspace",
    "in_git_work_tree",
    "run_step",
    "workspace_state",
]
