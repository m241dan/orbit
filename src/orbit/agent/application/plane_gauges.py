"""The live gauges a control plane reports: the agents running, the tasks waiting for a coder's
slot and those pending, the frontends connected and the bytes of the events stored, each read
as metrics are collected."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from orbit.agent.application.coder_slots import CoderSlots
from orbit.agent.application.connection import Connections
from orbit.agent.application.workspace_host import WorkspaceHost
from orbit.agent.domain.gauges import agents_by_role
from orbit.protocol import AgentRef
from orbit.tracing import Gauges, Reading

ROLE = "orbit.role"
WORKSPACE = "orbit.workspace.id"


@dataclass(frozen=True)
class PlaneGauges:
    """The gauges of a control plane serving the workspaces whose hosts ``hosts`` gives, with
    the ``agents`` running across them, its ``coders``' slots and its ``lobby``, the connections
    attached to no workspace.

    Metrics may be collected on another thread while the control plane changes what they read,
    so each read only takes copies, made at once, and counts what they hold."""

    hosts: Callable[[], Sequence[WorkspaceHost]]
    agents: Callable[[], Sequence[AgentRef]]
    coders: CoderSlots
    lobby: Connections

    def register(self, gauges: Gauges) -> None:
        """Register every gauge with ``gauges``."""
        gauges.register(
            "orbit.agents.active",
            "{agent}",
            "Agents running, by role and workspace.",
            self.agents_active,
        )
        gauges.register(
            "orbit.queue.depth", "{task}", "Tasks waiting for a coder's slot.", self.queue_depth
        )
        gauges.register(
            "orbit.tasks.pending",
            "{task}",
            "Pending tasks in each workspace's open session.",
            self.tasks_pending,
        )
        gauges.register(
            "orbit.frontends.connected",
            "{connection}",
            "Frontends connected whose Hello was welcomed.",
            self.frontends_connected,
        )
        gauges.register(
            "orbit.event_log.size",
            "By",
            "Bytes the logs of each workspace's sessions hold in the store.",
            self.event_log_size,
        )

    def agents_active(self) -> list[Reading]:
        """How many agents run in each role in each workspace served: each role it configures,
        even with none running, and any other role running there."""
        roles = {host.workspace_id: host.roles for host in self.hosts()}
        running = ((agent.workspace, agent.role) for agent in self.agents())
        counts = agents_by_role(roles, running).items()
        return [(count, {ROLE: role, WORKSPACE: workspace}) for (workspace, role), count in counts]

    def queue_depth(self) -> list[Reading]:
        """How many tasks wait in line for a coder's slot, across the workspaces served."""
        return [(self.coders.waiting, {})]

    def tasks_pending(self) -> list[Reading]:
        """How many tasks are pending in each workspace's open session."""
        return [(host.pending_tasks, {WORKSPACE: host.workspace_id}) for host in self.hosts()]

    def frontends_connected(self) -> list[Reading]:
        """How many connections had a ``Hello`` welcomed, attached to a workspace or not."""
        places = [self.lobby, *(host.connections for host in self.hosts())]
        return [(sum(place.welcomed for place in places), {})]

    def event_log_size(self) -> list[Reading]:
        """The bytes the logs of each workspace's sessions hold in the store, as it last knew
        them."""
        return [(host.log_size, {WORKSPACE: host.workspace_id}) for host in self.hosts()]
