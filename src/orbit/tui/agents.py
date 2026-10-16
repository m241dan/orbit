"""The agents pane: the agents running now, what each is doing, and whether review is paused."""

from collections.abc import Mapping
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets.option_list import Option

from orbit.protocol import Event, ReviewStatus
from orbit.tui.view import AgentRow, agent_line, review_line, track_agents
from orbit.tui.widgets import ListPane

EMPTY_LABEL = "No agents running"


class AgentsPane(ListPane):
    """Running agents, one per row. Activated like the task list: j/k move, ``c`` cancels the
    highlighted agent's work and escape returns to window navigation."""

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
        self.rows: tuple[AgentRow, ...] = ()
        self._numbers: Mapping[str, str] = {}
        self._shown: tuple[tuple[str, str], ...] = ()  # Each row's agent id and label.

    def on_mount(self) -> None:
        self.border_title = "Agents"
        self._redraw()

    def action_cancel_agent(self) -> None:
        if self.highlighted is not None and self.highlighted < len(self.rows):
            self.post_message(self.Cancel(self.rows[self.highlighted].agent_id))

    def track(self, event: Event) -> None:
        """Follow the agents through ``event``."""
        rows = track_agents(self.rows, event)
        if rows != self.rows:
            self.rows = rows
            self._redraw()

    def number_tasks(self, numbers: Mapping[str, str]) -> None:
        """Task ids' numbers in the task list, to show agents' tasks by; redraws the pane
        only if one of its agents' numbers changed."""
        changed = self._shown_numbers(numbers) != self._shown_numbers(self._numbers)
        self._numbers = numbers
        if changed:
            self._redraw()

    def _shown_numbers(self, numbers: Mapping[str, str]) -> list[str | None]:
        return [numbers.get(row.task_id) for row in self.rows]

    def show_review(self, status: ReviewStatus) -> None:
        self.border_subtitle = review_line(status)

    def _redraw(self) -> None:
        """Show the rows: the same agents in the same order have the rows whose label changed
        relabelled in place; agents coming, going or moving rebuild the rows."""
        shown = tuple(
            (row.agent_id, agent_line(row, self._numbers.get(row.task_id))) for row in self.rows
        )
        before, self._shown = self._shown, shown
        if shown and [agent for agent, _ in before] == [agent for agent, _ in shown]:
            self._relabel(before)
        else:
            self._rebuild()

    def _relabel(self, before: tuple[tuple[str, str], ...]) -> None:
        """Relabel the rows whose label differs from ``before``'s."""
        for index, ((_, label), (_, old)) in enumerate(zip(self._shown, before, strict=True)):
            if label != old:
                self.replace_prompt(index, label)

    def _rebuild(self) -> None:
        highlight = self.highlighted
        self.clear_options()
        if not self._shown:
            self.add_option(Option(EMPTY_LABEL, disabled=True))
            return
        self.add_options([label for _, label in self._shown])
        if highlight is not None:
            self.highlighted = min(highlight, len(self._shown) - 1)
