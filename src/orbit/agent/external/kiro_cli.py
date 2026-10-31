"""Agent launcher backed by Kiro's CLI (``kiro-cli chat``).

Each turn runs ``kiro-cli chat --output-format stream-json`` as a Kiro agent of its own, whose
config, written just before it starts and removed once it ends however it ends, connects it to
orbit's MCP server alone, tells it orbit's rules and its role's instructions, and gives it the
orbit tools its role is granted and the built-ins its tool access allows. Its first message, the
prompt, tells it to fetch its task. The stream it prints is translated into protocol events, and
later turns of a conversation pass ``--resume-id <session id>`` so Kiro carries it on.

Kiro reports when its model takes no effort; a notice says so of an agent given one anyway, once
per role in each session.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import get_args

from orbit.agent.application.ports import AgentLaunch, ResumeFailed
from orbit.agent.domain.runtimes import Capabilities, Failure, FullIdRule
from orbit.agent.domain.usage_limits import kiro_failure
from orbit.agent.external.cli_turns import (
    OTLP_HEADER_VARIABLES,
    CliCommand,
    Conversations,
    crashed,
    followed,
    started,
)
from orbit.agent.external.kiro_agents import KiroAgentFiles, kiro_agent_config
from orbit.agent.external.kiro_stream import KiroTranslation
from orbit.protocol import (
    Effort,
    ErrorOccurred,
    Event,
    Notice,
    ToolAccess,
    TurnStarted,
)

CHAT_ARGS = ("chat", "--output-format", "stream-json")
# The engine configs and the stream were captured on, rather than whatever Kiro defaults to
# next; and orbit's MCP server must start, or the run fails before it spends credits.
PINNED_ARGS = ("--agent-engine", "v2", "--require-mcp-startup")
MCP_UNREACHABLE = 3
"""What ``kiro-cli`` exits with when an MCP server it requires doesn't start."""
INIT_STAGE = "init"
"""The stage a run fails at when it never starts, as one resuming a session that's gone does."""
DEFAULT_MODEL = "auto"
"""The model Kiro picks when given none."""
MISSING_HINT = "Is the Kiro CLI (kiro-cli) installed and on PATH?"

# Kiro resumes sessions, reports credits, and takes every tool access and effort, though a model
# may ignore the effort; it caps no turns.
CAPABILITIES = Capabilities(
    resume=True,
    usage=True,
    max_turns=False,
    tool_access=frozenset(get_args(ToolAccess)),
    effort=frozenset(get_args(Effort)),
)
# Kiro's model ids, such as glm-5 and claude-sonnet-4.5; a bare word, such as opus, is more
# likely a mistyped alias.
FULL_MODEL_IDS = FullIdRule(
    r"[a-z0-9.-]*-[a-z0-9.-]*",
    "of lowercase letters, digits, dots and dashes, with at least one dash",
)
# Orbit's rules, as Claude's agents are told them, naming orbit's tools as Kiro does.
AGENT_RULES = (
    "You are an agent working for orbit, which hands out tasks and verifies the work. Talk to"
    " orbit only through its MCP tools (@orbit)."
)


def kiro_args(
    command: Sequence[str], name: str, launch: AgentLaunch, resume: str | None
) -> list[str]:
    """The arguments running ``command`` as the Kiro agent ``name`` for ``launch``, with its
    model and effort if it has them, resuming the Kiro session ``resume`` if given, and told the
    launch's prompt."""
    settings = launch.settings
    args = [*command, *CHAT_ARGS, "--agent", name, *PINNED_ARGS]
    if settings.model:
        args += ["--model", settings.model]
    if settings.effort:
        args += ["--effort", settings.effort]
    if resume:
        args += ["--resume-id", resume]
    # A prompt starting with a dash isn't taken for a flag.
    return [*args, "--", launch.prompt]


def kiro_env() -> dict[str, str]:
    """The environment an agent's ``kiro-cli`` runs in: orbit's own, without the OTLP headers it
    may hold, as they may hold the key to orbit's collector, which an agent could leak."""
    return {name: value for name, value in os.environ.items() if name not in OTLP_HEADER_VARIABLES}


class _EffortNotes:
    """Notes, once per role in each session, that Kiro's model ignores the effort an agent of
    the role was given."""

    def __init__(self) -> None:
        self._told: set[tuple[str, str]] = set()  # Session id and role.

    def note(self, launch: AgentLaunch) -> list[Event]:
        """The note that ``launch``'s model ignores its effort, unless its role was told so in
        its session already."""
        role = launch.agent.role
        if (launch.session_id, role) in self._told:
            return []
        self._told.add((launch.session_id, role))
        model = launch.settings.model or DEFAULT_MODEL
        return [Notice(message=f"kiro's model {model} ignores effort; {role} runs without it.")]


@dataclass(frozen=True)
class _Kiro:
    """How agents run on Kiro: as ``command``, with their configs among ``files``, a model
    ignoring an agent's effort noted by ``effort``."""

    command: tuple[str, ...]
    files: KiroAgentFiles
    effort: _EffortNotes


class KiroCliRunner(Conversations):
    """Runs agents as Kiro sessions connected to orbit, run as ``command``, each agent with its
    config among ``files``, ``~/.kiro/agents``' by default. Its full model ids are those
    ``full_model_ids`` takes."""

    capabilities = CAPABILITIES
    full_model_ids = FULL_MODEL_IDS

    def __init__(
        self, command: Sequence[str] = ("kiro-cli",), *, files: KiroAgentFiles | None = None
    ) -> None:
        super().__init__()
        self._kiro = _Kiro(tuple(command), files or KiroAgentFiles(), _EffortNotes())

    async def run_agent(self, launch: AgentLaunch) -> AsyncIterator[Event]:
        """Run the agent as a new Kiro session connected to orbit, as ``launch`` says."""
        async for event in self._turn(launch, None).run(launch.agent.id):
            yield event

    async def converse(self, turn_id: str, launch: AgentLaunch) -> AsyncIterator[Event]:
        """A turn of the agent's conversation: its first starts a Kiro session, later ones
        resume it, each agent's apart from the others'. A resuming turn that fails to start
        before reporting its session raises ResumeFailed."""
        agent_id = launch.agent.id
        turn = self._turn(launch, self.conversation(agent_id))
        try:
            async for event in turn.run(turn_id):
                yield event
        finally:
            self.remember(agent_id, turn.session_id)

    def failure(self, message: str) -> Failure | None:
        """The failure Kiro's error ``message`` tells, as :func:`kiro_failure` says."""
        return kiro_failure(message)

    def sweep(self, now: datetime) -> None:
        """Remove the configs crashed runs left behind over a day before ``now``."""
        self._kiro.files.sweep(now)

    def _turn(self, launch: AgentLaunch, resume: str | None) -> _Turn:
        """A turn of ``launch``'s agent, resuming the Kiro session ``resume`` if given."""
        return _Turn(self._kiro, launch, resume)


class _Turn:
    """One run of Kiro, as ``kiro`` runs it, as ``launch``'s agent, resuming the Kiro session
    ``resume`` if given: translates what it says, noting the session it ran in and a model
    ignoring the agent's effort."""

    def __init__(self, kiro: _Kiro, launch: AgentLaunch, resume: str | None) -> None:
        self._kiro = kiro
        self._launch = launch
        self._resume = resume
        self.session_id: str | None = None

    async def run(self, turn_id: str) -> AsyncIterator[Event]:
        yield TurnStarted(turn_id=turn_id, agent_id=self._launch.agent.id)
        # Cancelled, or the CLI failed: leaving the stack stops it and whatever it started, then
        # removes its config.
        async with contextlib.AsyncExitStack() as stack:
            process = await self._start(stack)
            events = partial(self._events, KiroTranslation(turn_id))
            async for event in followed(process, events, self._crash):
                yield event

    async def _start(self, stack: contextlib.AsyncExitStack) -> asyncio.subprocess.Process:
        """The running CLI, stopped when ``stack`` closes, its config removed after it."""
        name = self._configure(stack)
        args = kiro_args(self._kiro.command, name, self._launch, self._resume)
        command = CliCommand(tuple(args), kiro_env(), self._launch.cwd, MISSING_HINT)
        return await started(stack, command, asyncio.subprocess.DEVNULL)

    def _configure(self, stack: contextlib.AsyncExitStack) -> str:
        """Write the agent's config, removed when ``stack`` closes; returns the name Kiro knows
        it by."""
        config = kiro_agent_config(AGENT_RULES, self._launch)
        files = self._kiro.files
        try:
            name = files.write(self._launch.agent.id, config)
        except OSError as error:
            raise RuntimeError(f"Couldn't write the agent's Kiro config: {error}") from None
        stack.callback(files.remove, name)
        return name

    async def _events(
        self, translation: KiroTranslation, stdout: asyncio.StreamReader
    ) -> AsyncIterator[Event]:
        """The events of the stream on ``stdout``, then those its end completes."""
        async for raw in stdout:
            for event in self._read(translation, raw.decode(errors="replace")):
                yield event
        for event in translation.end():
            yield self._own(event)

    def _read(self, translation: KiroTranslation, line: str) -> list[Event]:
        """The events ``line`` completes, as the agent's, but for the turn's start, already
        given, then the note of its effort ignored if that's what the stream tells.

        Raises ResumeFailed, rather than giving its error, for a resumed run that never started.
        """
        events = [self._own(e) for e in translation.feed(line) if not isinstance(e, TurnStarted)]
        for event in events:
            self._check_resumed(translation, event)
        self.session_id = translation.session_id
        return [*events, *self._effort_notes(translation)]

    def _check_resumed(self, translation: KiroTranslation, event: Event) -> None:
        """Raise ResumeFailed, with its message, if ``event`` is the error of a run that resumed
        a session and failed to start before it reported a session, as ``translation`` shows."""
        if isinstance(event, ErrorOccurred) and self._unresumed(translation):
            raise ResumeFailed(event.message)

    def _unresumed(self, translation: KiroTranslation) -> bool:
        """Whether ``translation`` shows a run resuming a session failing to start before it
        reported a session."""
        failed_at_start = translation.error_stage == INIT_STAGE
        return self._resume is not None and failed_at_start and translation.session_id is None

    def _effort_notes(self, translation: KiroTranslation) -> list[Event]:
        """The note that the agent's model ignores its effort, if it has one and ``translation``
        shows reasoning unavailable, unless its role was told so in its session already."""
        if not (self._launch.settings.effort and translation.reasoning_unavailable):
            return []
        return self._kiro.effort.note(self._launch)

    def _own(self, event: Event) -> Event:
        """``event``, as the agent's."""
        return event.model_copy(update={"agent_id": self._launch.agent.id})

    def _crash(self, status: int, stderr: str) -> RuntimeError:
        """The error for a CLI that exited with ``status`` and ``stderr`` without reporting the
        run's end."""
        if status == MCP_UNREACHABLE:
            return RuntimeError(
                f"Kiro couldn't connect to orbit's MCP server at {self._launch.url}"
            )
        return crashed("kiro-cli", status, stderr)
