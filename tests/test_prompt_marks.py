"""The prompt's marks, without Textual: whom it talks to and whether that's busy or the orbit
read-only, and the settings under it."""

from orbit.protocol import Settings
from orbit.tui.view.prompt import PromptMarks


class Prompt:
    """The prompt, as its marks show on it: its title, subtitle and placeholder."""

    def __init__(self) -> None:
        self.title = ""
        self.subtitle = ""
        self.placeholder = ""
        self.marks = PromptMarks(self)

    def show_title(self, title: str) -> None:
        self.title = title

    def show_subtitle(self, subtitle: str) -> None:
        self.subtitle = subtitle

    def show_placeholder(self, placeholder: str) -> None:
        self.placeholder = placeholder


def test_the_title_marks_the_planner_busy_and_the_orbit_read_only_each_until_it_isnt() -> None:
    prompt = Prompt()
    prompt.marks.show_busy(False)
    assert prompt.title == "Prompt · planner"
    prompt.marks.show_busy(True)
    assert prompt.title == "Prompt · planner · busy"
    prompt.marks.show_read_only(True)
    assert prompt.title == "Prompt · planner · busy · read-only"
    prompt.marks.show_busy(False)
    assert prompt.title == "Prompt · planner · read-only"
    prompt.marks.show_read_only(False)
    assert prompt.title == "Prompt · planner"
    assert (prompt.marks.busy, prompt.marks.read_only) == (False, False)


def test_the_settings_show_under_the_prompt() -> None:
    prompt = Prompt()
    prompt.marks.show_settings(Settings(model="opus", tool_access="restricted"))
    assert prompt.subtitle == "planner's model: opus · new tasks' tools: restricted"


def test_the_title_and_placeholder_name_the_role_talked_to_keeping_the_marks() -> None:
    prompt = Prompt()
    prompt.marks.show_busy(True)
    prompt.marks.show_target("architect", "Architect")
    assert prompt.title == "Prompt · architect · busy"
    assert prompt.placeholder == "Press i to talk to Architect, esc to stop typing"
    prompt.marks.show_settings(Settings(effort="max"))
    assert prompt.subtitle == (
        "architect's model: default · effort: max · new tasks' tools: restricted"
    )
