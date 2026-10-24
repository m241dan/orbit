"""The agents pane: the agents running now, what each is doing, and whether review is paused."""

from collections.abc import Mapping, Sequence
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets.option_list import Option

from orbit.protocol import Event
from orbit.tui.list_pane import ListPane
from orbit.tui.view.agents import NO_AGENTS_LABEL, AgentRow, AgentsList


class AgentsPane(ListPane):
    """Shows the :class:`AgentsList` it holds as ``agents``, one row per running agent.
    Activated like the task list: j/k move, ``c`` cancels the highlighted agent's work and
    escape returns to window navigation."""

    DEFAULT_CSS = """
    AgentsPane {
        height: auto;
        max-height: 12;
        border: round $primary;
        padding: 0 1;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "cancel_agent", show=False),
    ]

    class Cancel(Message):
        """The user asked to cancel agent ``agent_id``'s work."""

        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.agents = AgentsList(self)

    @property
    def rows(self) -> tuple[AgentRow, ...]:
        """The agents running, a row each."""
        return self.agents.rows

    def on_mount(self) -> None:
        self.border_title = "Agents"
        self.agents.show()

    def action_cancel_agent(self) -> None:
        self.agents.cancel()

    def track(self, event: Event) -> None:
        """Follow the agents through ``event``, as the list takes it."""
        self.agents.track(event)

    def number_tasks(self, numbers: Mapping[str, str]) -> None:
        """Show agents' tasks by their ``numbers`` in the task list, as the list takes them."""
        self.agents.number_tasks(numbers)

    def list_agents(self, rows: Sequence[tuple[str, str]]) -> None:
        self.show_rows(rows, None if rows else Option(NO_AGENTS_LABEL, disabled=True))

    def show_review_line(self, line: str) -> None:
        self.border_subtitle = line

    def ask_to_cancel(self, agent_id: str) -> None:
        self.post_message(self.Cancel(agent_id))
