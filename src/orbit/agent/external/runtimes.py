"""What each runtime this version of orbit knows can do, as its launcher declares it: what the
roles are held to as they're loaded."""

from orbit.agent.domain.runtimes import CLAUDE, Capabilities
from orbit.agent.external.claude_cli import ClaudeCliRunner

RUNTIME_CAPABILITIES: dict[str, Capabilities] = {CLAUDE: ClaudeCliRunner.capabilities}
"""What each runtime this version of orbit knows can do, by name, in order."""
