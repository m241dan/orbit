"""orbit status: what the control plane an orbit serve runs is doing, asked once and printed."""

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import typer

from orbit import __version__
from orbit.agent import TokenFile, resolve_config_dir, resolve_state_dir
from orbit.cli.chat import (
    _LOCAL_ERRORS,
    Discovered,
    _ClientFiles,
    _joined_client,
    _joining,
    _refuse_state_dir,
    _target,
    discover_control_plane,
)
from orbit.cli.errors import _configured, _exit_on
from orbit.cli.options import (
    CaOption,
    ClientCertOption,
    ClientKeyOption,
    ConfigDirOption,
    JsonOption,
    RemoteOption,
    StateDirOption,
    StatusConnectOption,
    TokenFileOption,
)
from orbit.cli.status_report import StatusReport, status_json, status_report, status_text
from orbit.client import HandshakeRefused, TokenRejected, messages
from orbit.protocol import (
    CONTROL_PLANE_STREAM,
    PROTOCOL_VERSION,
    AgentsChanged,
    Catalog,
    CommandEnvelope,
    CommandRefused,
    Connected,
    Ended,
    Event,
    EventEnvelope,
    HandshakeFailed,
    Hello,
    Link,
    LinkState,
    ProtocolError,
    Reconnecting,
    Session,
    Stopped,
    TokenRefused,
    UsageTotals,
    Watch,
    Welcome,
)

STATUS_SECONDS = 10.0
"""How long the control plane has to answer every question orbit status asks, in seconds."""

NOT_RUNNING = "No orbit serve is running for this account."
"""What orbit status says when it finds no control plane running here."""

_HELLO = CommandEnvelope(
    command_id="status",
    payload=Hello(client=f"orbit-status {__version__}", protocol_version=PROTOCOL_VERSION),
)
_WATCH = CommandEnvelope(command_id="watch", payload=Watch(streams=(CONTROL_PLANE_STREAM,)))


def status(
    ctx: typer.Context,
    as_json: JsonOption = False,
    state: StateDirOption = None,
    config_dir: ConfigDirOption = None,
    connect: StatusConnectOption = None,
    remote: RemoteOption = None,
    token_file: TokenFileOption = None,
    ca: CaOption = None,
    cert: ClientCertOption = None,
    key: ClientKeyOption = None,
) -> None:
    """Show what the control plane orbit serve runs is doing: its workspaces and their
    sessions, the agents running and what turns have cost, today and over its lifetime.

    It asks the orbit serve running here, or the one --connect or --remote names, showing it
    the token or client certificate orbit chat would, and prints the answer, as text or with
    --json as one JSON object. Exits with status 1 if no orbit serve is running here, and 2 if
    the control plane can't be asked: a connection, TLS or credential error, or no answer
    within 10 seconds.
    """
    # #lizard forgives(parameter_count): Typer takes each of the command's options as a parameter.
    user_config = resolve_config_dir(config_dir, os.environ)
    given = _ClientFiles(token_file, ca, cert, key)
    target = _target(connect, remote, user_config, given)
    if target is None:
        found = _running(resolve_state_dir(state, os.environ), given, user_config)
    else:
        _refuse_state_dir(ctx, target.flag)
        found = Discovered(target.url, target.files)
    client = _joined_client(found.url, _joining(ctx, found.files, user_config, None))
    with _exit_on(_Unreachable):
        report = asyncio.run(_reported(client, found.url, STATUS_SECONDS))
    typer.echo(status_json(report) if as_json else status_text(report))


def _running(state: Path, given: _ClientFiles, config_dir: Path) -> Discovered:
    """The control plane running on the ``state`` directory, as :func:`discover_control_plane`
    finds it, shown the files ``given`` over those in the config directory ``config_dir``.

    Exits with status 1 if there's none, and 2 if the token, the user config, the CA or the
    client certificate can't be used, the control plane turns the token away, or refuses the
    handshake.
    """
    token_file = given.token_file or TokenFile(config_dir).path
    refused = messages.TOKEN_REFUSED.format(path=token_file)
    with (
        _exit_on(TokenRejected, message=lambda _: refused),
        _exit_on(HandshakeRefused, *_LOCAL_ERRORS),
    ):
        found = _configured(lambda: discover_control_plane(state, given, config_dir))
    if found is None:
        typer.echo(NOT_RUNNING, err=True)
        raise typer.Exit(1)
    return found


class _Unreachable(Exception):
    """The control plane couldn't be asked; the message says why."""


class _Client(Session, Link, Protocol):
    """A session with a control plane, whose link reports how it stands."""


type _Item = EventEnvelope | LinkState | None
"""What comes from a client while it's asked: an event, a state its link moved to, or ``None``
once its events end."""


async def _reported(client: _Client, url: str, seconds: float) -> StatusReport:
    """What the control plane at ``url`` reports through ``client``, which is closed after.

    Raises :class:`_Unreachable` if the link fails, a command is refused or the events end
    before every answer has come, or they don't all come within ``seconds``.
    """
    try:
        async with asyncio.timeout(seconds):
            return await _asked(client, url)
    except TimeoutError:
        raise _Unreachable(
            f"The control plane at {url} didn't answer within {seconds:g} seconds."
        ) from None
    finally:
        await client.close()


async def _asked(client: _Client, url: str) -> StatusReport:
    """What the control plane at ``url`` reports through ``client``, greeted, then asked to
    tell the control-plane stream.

    Raises :class:`_Unreachable` if the link fails, a command is refused or the events end
    before every answer has come.
    """
    items: asyncio.Queue[_Item] = asyncio.Queue()
    client.watch(items.put_nowait)
    pumping = asyncio.create_task(_pumped(client, items))
    try:
        await client.send(_HELLO)
        await client.send(_WATCH)
        answers = _Answers()
        while (report := answers.report(url)) is None:
            answers = answers.taking(await items.get(), url)
        return report
    finally:
        pumping.cancel()
        with suppress(asyncio.CancelledError):
            await pumping


async def _pumped(client: _Client, items: asyncio.Queue[_Item]) -> None:
    """Put each event ``client`` brings in ``items``, then ``None`` once they end."""
    # An event that can't be read ends the link too, which reports it, saying why.
    with suppress(ProtocolError):
        async for envelope in client.events():
            items.put_nowait(envelope)
    items.put_nowait(None)


@dataclass(frozen=True)
class _Answers:
    """The latest ``Welcome``, ``Catalog``, ``AgentsChanged`` and ``UsageTotals`` that came, each
    ``None`` until one has."""

    welcome: Welcome | None = None
    catalog: Catalog | None = None
    agents: AgentsChanged | None = None
    totals: UsageTotals | None = None

    def report(self, url: str) -> StatusReport | None:
        """The report these answers make of the control plane at ``url``; none until every one
        has come."""
        welcome, catalog, agents, totals = self.welcome, self.catalog, self.agents, self.totals
        if welcome is None or catalog is None or agents is None or totals is None:
            return None
        return status_report(welcome, catalog, agents, totals, url)

    def taking(self, item: _Item, url: str) -> _Answers:
        """These answers with ``item``, from the client of the control plane at ``url``, taken
        in, an answer in place of the one before it.

        Raises :class:`_Unreachable` if it says the link failed or a command was refused, or
        it's the end of the events.
        """
        if item is None:
            raise _Unreachable(
                f"The connection to the control plane at {url} closed before it answered."
            )
        if not isinstance(item, EventEnvelope):
            return self._linked(item)
        if isinstance(item.payload, CommandRefused):
            raise _Unreachable(item.payload.message)
        return self._with(item.payload)

    def _linked(self, state: LinkState) -> _Answers:
        """These answers, the link having moved to ``state``.

        Raises :class:`_Unreachable` unless it's up.
        """
        failure = _link_failure(state)
        if failure is not None:
            raise _Unreachable(failure)
        return self

    def _with(self, event: Event) -> _Answers:
        """These answers with ``event`` in place of the one before it, if it's one of them."""
        match event:
            case Welcome():
                return replace(self, welcome=event)
            case Catalog():
                return replace(self, catalog=event)
            case AgentsChanged():
                return replace(self, agents=event)
            case UsageTotals():
                return replace(self, totals=event)
        return self


def _link_failure(state: LinkState) -> str | None:
    """What says the link, now in ``state``, failed before every answer came; none while it's
    up."""
    if isinstance(state, Connected):
        return None
    if isinstance(state, TokenRefused | HandshakeFailed):
        return state.message
    return _lost(state)


def _lost(state: Ended | Stopped | Reconnecting) -> str:
    """What says the link was lost, or never made, as ``state`` says."""
    if isinstance(state, Ended):
        because = f": {state.reason}." if state.reason else "."
        return f"The connection to the control plane at {state.url} ended{because}"
    if isinstance(state, Stopped):
        return f"The control plane at {state.url} is shutting down."
    return f"Can't reach the control plane at {state.url}."
