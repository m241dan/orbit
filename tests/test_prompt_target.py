"""The prompt talking to the conversational role whose tab is shown, without Textual: a tab per
conversational role, whom the prompt sends to and how it's marked, and what ctrl+t, ctrl+x,
ctrl+n and the settings pickers act on."""

from orbit.protocol import (
    BUSY,
    NOT_CONFIGURED,
    PLANNER,
    AgentEntry,
    AgentStarted,
    AssistantMessage,
    BusyChanged,
    CancelTurn,
    Catalog,
    ConversationReset,
    MessageSent,
    ResetConversation,
    SendMessage,
    Settings,
    SettingsChanged,
    UpdateSettings,
    WorkspaceEntry,
)
from orbit.tui.view.seats import PLANNER_SEAT
from tests.frontend_fakes import Shell
from tests.session_apps import CURRENT, with_agents
from tests.tui_fakes import SNAPSHOT

ARCHITECT = "architect"
DESIGN = "design"
"""A project's own conversational role, and the lane it answers in."""

PLANNER_ENTRY = AgentEntry(name=PLANNER, label="Planner", conversational=True, lane="planner")
CODER_ENTRY = AgentEntry(name="coder", label="Coder", conversational=False)
ARCHITECT_ENTRY = AgentEntry(name=ARCHITECT, label="Architect", conversational=True, lane=DESIGN)

TALKING = with_agents(PLANNER_ENTRY, CODER_ENTRY, ARCHITECT_ENTRY)
"""A catalog listing the planner, the coder and the architect."""

BUILT_IN = with_agents(PLANNER_ENTRY, CODER_ENTRY)
"""A catalog listing the built-in roles' planner and coder alone."""

CRITIC = "critic"
CRITIC_ENTRY = AgentEntry(name=CRITIC, label="Critic", conversational=True, lane=DESIGN)
"""Another conversational role, answering in the architect's lane."""
BUSY_CRITIC = f"● {CRITIC}"
"""The critic's tab while its lane is busy."""

SHARING = with_agents(*TALKING.agents, CRITIC_ENTRY)
"""A catalog listing the planner, the coder, and the architect and the critic in one lane."""

STRIP = ["All", "planner", "coder", "reviewer", "super-reviewer", "configurator"]

TALK_TO_PLANNER = "Press i to talk to Planner, esc to stop typing"
TALK_TO_ARCHITECT = "Press i to talk to Architect, esc to stop typing"


async def talking() -> Shell:
    """A frontend whose catalog lists the architect beside the planner, the planner's and the
    architect's settings reported."""
    shell = Shell()
    await shell.feed(
        TALKING,
        SettingsChanged(settings=Settings(model="opus")),
        SettingsChanged(role=ARCHITECT, settings=Settings(model="haiku")),
    )
    return shell


async def test_each_conversational_role_has_a_tab_after_the_built_in_ones() -> None:
    shell = await talking()
    assert shell.tab_strip == [*STRIP, ARCHITECT]


async def test_a_conversational_roles_tab_comes_before_the_agents_own_tabs() -> None:
    shell = Shell()
    await shell.feed(AgentStarted(agent_id="agent-1", role="checker", task_id="startup"))
    await shell.feed(TALKING)
    assert shell.tab_strip == [*STRIP, ARCHITECT, "checker"]
    await shell.cycle(-1, 2)
    assert shell.active_tab == ARCHITECT


async def test_on_a_conversational_roles_tab_the_prompt_talks_to_it() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    assert shell.marks.placeholder == TALK_TO_ARCHITECT
    assert shell.marks.title == "Prompt · architect"
    await shell.submit("design it")
    assert shell.wire.changes == [SendMessage(agent=ARCHITECT, text="design it")]
    assert shell.tab_entries(ARCHITECT) == ["design it"]
    assert shell.tab_entries(PLANNER) == []
    assert shell.marks.title == "Prompt · architect · busy"
    assert shell.tab_strip[-1] == "● architect"


async def test_on_any_other_tab_the_prompt_talks_to_the_planner() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    await shell.click_tab("All")
    assert shell.marks.placeholder == TALK_TO_PLANNER
    assert shell.marks.title == "Prompt · planner"
    await shell.submit("plan")
    await shell.feed(BusyChanged(busy=False, lane="planner"))
    await shell.click_tab("coder")
    await shell.submit("more")
    assert shell.wire.changes == [
        SendMessage(agent=PLANNER, text="plan"),
        SendMessage(agent=PLANNER, text="more"),
    ]
    assert shell.tab_entries(PLANNER) == ["plan", "more"]


async def test_each_role_is_busy_in_its_own_lane() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    await shell.submit("design it")
    await shell.submit("again")
    assert shell.app.warnings == ["Architect is busy"]
    await shell.click_tab("All")
    assert shell.marks.title == "Prompt · planner"
    await shell.submit("plan")
    await shell.feed(BusyChanged(busy=False, lane=DESIGN))
    assert shell.marks.title == "Prompt · planner · busy"
    await shell.click_tab(ARCHITECT)
    assert shell.marks.title == "Prompt · architect"
    assert shell.tab_strip[1:2] == ["● planner"]
    assert shell.wire.changes == [
        SendMessage(agent=ARCHITECT, text="design it"),
        SendMessage(agent=PLANNER, text="plan"),
    ]


async def test_a_refused_prompt_frees_the_lane_it_was_sent_to() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    await shell.submit("design it")
    await shell.click_tab("All")
    await shell.refuse(NOT_CONFIGURED, "architect is disabled in this project's configuration.")
    await shell.click_tab(ARCHITECT)
    assert shell.marks.title == "Prompt · architect"
    assert shell.prompt.text == "design it"


async def sharing() -> Shell:
    """A frontend whose catalog lists the architect and the critic in one lane, the architect
    answering a message another frontend sent it."""
    shell = Shell()
    await shell.feed(SHARING, BusyChanged(busy=True, lane=DESIGN))
    await shell.feed(MessageSent(command_id="other", agent=ARCHITECT, text="design it"))
    return shell


async def test_a_prompt_while_another_role_in_its_lane_answers_names_the_one_answering() -> None:
    shell = await sharing()
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("critique it")
    assert shell.app.warnings == ["Architect is busy"]
    assert shell.wire.changes == []
    assert shell.prompt.text == "critique it"


async def test_a_prompt_to_the_role_answering_names_that_role() -> None:
    shell = Shell()
    await shell.feed(SHARING, BusyChanged(busy=True, lane=DESIGN))
    await shell.feed(MessageSent(command_id="other", agent=CRITIC, text="critique it"))
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("again")
    await shell.click_tab(f"● {ARCHITECT}")
    await shell.submit("design it")
    assert shell.app.warnings == ["Critic is busy", "Critic is busy"]


async def test_a_prompt_sent_here_names_its_role_once_the_backend_echoes_it() -> None:
    shell = Shell()
    await shell.feed(SHARING)
    await shell.click_tab(ARCHITECT)
    await shell.submit("design it")
    await shell.feed(BusyChanged(busy=True, lane=DESIGN))
    sent = shell.wire.sent[-1].command_id
    await shell.feed(MessageSent(command_id=sent, agent=ARCHITECT, text="design it"))
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("critique it")
    assert shell.app.warnings == ["Architect is busy"]
    assert shell.tab_entries(ARCHITECT) == ["design it"]


async def test_a_message_to_a_role_in_another_lane_leaves_the_one_answering_named() -> None:
    shell = await sharing()
    await shell.feed(BusyChanged(busy=True, lane="planner"))
    await shell.feed(MessageSent(command_id="more", agent=PLANNER, text="plan"))
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("critique it")
    await shell.click_tab("All")
    await shell.submit("plan more")
    assert shell.app.warnings == ["Architect is busy", "Planner is busy"]


async def test_a_message_to_a_role_not_listed_names_no_one_answering() -> None:
    shell = Shell()
    await shell.feed(SHARING, BusyChanged(busy=True, lane=DESIGN))
    await shell.feed(MessageSent(command_id="other", agent="muse", text="inspire"))
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("critique it")
    assert shell.app.warnings == ["Critic is busy"]


async def test_once_its_lane_is_free_the_role_answering_there_is_forgotten() -> None:
    shell = await sharing()
    await shell.feed(BusyChanged(busy=False, lane=DESIGN))
    await shell.feed(BusyChanged(busy=True, lane=DESIGN))
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("critique it")
    assert shell.app.warnings == ["Critic is busy"]


async def test_a_snapshot_leaves_the_role_answering_in_a_busy_lane_unknown() -> None:
    shell = await sharing()
    await shell.feed(SNAPSHOT.model_copy(update={"busy": {DESIGN: True}}))
    await shell.click_tab(BUSY_CRITIC)
    await shell.submit("critique it")
    assert shell.app.warnings == ["Critic is busy"]


async def test_a_prompt_refused_as_busy_leaves_its_lane_busy_until_the_backend_frees_it() -> None:
    shell = Shell()
    await shell.feed(SHARING)
    await shell.click_tab(CRITIC)
    await shell.submit("critique it")
    await shell.refuse(BUSY, "The architect is already answering.")
    assert shell.marks.title == "Prompt · critic · busy"
    assert shell.tab_strip[-2:] == ["● architect", "● critic"]
    assert shell.prompt.text == "critique it"
    await shell.feed(BusyChanged(busy=False, lane=DESIGN))
    assert shell.marks.title == "Prompt · critic"
    assert shell.tab_strip[-2:] == [ARCHITECT, CRITIC]


async def test_ctrl_t_cycles_the_conversational_roles_tabs_then_goes_back() -> None:
    shell = await talking()
    await shell.cycle(1, 3)
    assert shell.active_tab == "reviewer"
    await shell.cycle_conversations()
    assert shell.active_tab == "planner"
    await shell.cycle_conversations()
    assert shell.active_tab == ARCHITECT
    assert shell.marks.placeholder == TALK_TO_ARCHITECT
    await shell.cycle_conversations()
    assert shell.active_tab == "reviewer"
    assert shell.marks.placeholder == TALK_TO_PLANNER


async def test_with_the_planner_alone_ctrl_t_toggles_its_tab() -> None:
    shell = Shell()
    await shell.feed(BUILT_IN)
    assert shell.tab_strip == STRIP
    await shell.cycle_conversations()
    assert shell.active_tab == "planner"
    await shell.cycle_conversations()
    assert shell.active_tab == "All"


async def test_ctrl_x_stops_the_shown_role_or_the_planner_elsewhere() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    await shell.frontend.stop_planner()
    await shell.click_tab("coder")
    await shell.frontend.stop_planner()
    assert shell.wire.changes == [CancelTurn(lane=DESIGN), CancelTurn(lane="planner")]


async def test_ctrl_n_asks_then_resets_the_shown_role() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    shell.frontend.reset_planner()
    [(title, message, yes, then)] = shell.app.questions
    assert (title, yes) == ("Reset the architect?", "reset")
    assert message.startswith("The architect's conversation ends")
    assert shell.wire.changes == []
    await then()
    assert shell.wire.changes == [ResetConversation(agent=ARCHITECT)]


async def test_ctrl_n_elsewhere_resets_the_planner() -> None:
    shell = await talking()
    shell.frontend.reset_planner()
    [(title, _, _, then)] = shell.app.questions
    assert title == "Reset the planner?"
    await then()
    assert shell.wire.changes == [ResetConversation(agent=PLANNER)]


async def test_a_reset_rules_off_the_roles_own_tab() -> None:
    shell = await talking()
    await shell.feed(ConversationReset(agent=ARCHITECT))
    assert shell.tab_entries(ARCHITECT) == ["architect reset"]
    assert shell.tab_entries(PLANNER) == []


async def test_the_roles_agent_talks_under_its_tab_marked_only_while_it_answers() -> None:
    shell = await talking()
    await shell.feed(
        AgentStarted(agent_id="agent-2", role=ARCHITECT, task_id="plan"),
        AssistantMessage(turn_id="t", text="Here's a design.", agent_id="agent-2"),
    )
    assert shell.tab_strip == [*STRIP, "architect *"]
    assert shell.tab_entries(ARCHITECT)[-1] == "Here's a design."


async def test_the_prompt_shows_the_shown_roles_settings() -> None:
    shell = await talking()
    assert shell.marks.subtitle == "planner's model: opus · new tasks' tools: restricted"
    await shell.click_tab(ARCHITECT)
    assert shell.marks.subtitle == "architect's model: haiku · new tasks' tools: restricted"
    await shell.feed(SettingsChanged(settings=Settings(model="sonnet")))
    assert shell.marks.subtitle == "architect's model: haiku · new tasks' tools: restricted"


async def test_a_role_with_no_settings_reported_shows_the_planners() -> None:
    shell = Shell()
    await shell.feed(TALKING, SettingsChanged(settings=Settings(model="opus")))
    await shell.click_tab(ARCHITECT)
    assert shell.marks.subtitle == "architect's model: opus · new tasks' tools: restricted"


async def test_a_snapshot_reports_each_roles_settings() -> None:
    shell = Shell()
    architects = Settings(model="haiku", effort="max")
    snapshot = SNAPSHOT.model_copy(update={"role_settings": {ARCHITECT: architects}})
    await shell.feed(TALKING, snapshot)
    assert shell.marks.subtitle == "planner's model: opus · new tasks' tools: restricted"
    await shell.click_tab(ARCHITECT)
    assert shell.marks.subtitle == (
        "architect's model: haiku · effort: max · new tasks' tools: restricted"
    )


async def test_picking_an_effort_on_a_roles_tab_sets_that_roles_settings_alone() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    shell.frontend.pick_effort()
    await shell.pickers.pick(3)
    assert shell.wire.changes == [
        UpdateSettings(role=ARCHITECT, settings=Settings(model="haiku", effort="high"))
    ]
    await shell.feed(SettingsChanged(role=ARCHITECT, settings=Settings(effort="high")))
    await shell.click_tab("All")
    assert shell.marks.subtitle == "planner's model: opus · new tasks' tools: restricted"


async def test_the_model_and_tool_pickers_set_the_shown_roles_settings() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    shell.frontend.pick_model()
    await shell.pickers.pick(0)
    shell.frontend.pick_tool_access()
    await shell.pickers.pick(2)
    await shell.click_tab("All")
    shell.frontend.pick_effort()
    await shell.pickers.pick(5)
    assert shell.wire.changes == [
        UpdateSettings(role=ARCHITECT, settings=Settings()),
        UpdateSettings(role=ARCHITECT, settings=Settings(model="haiku", tool_access="full")),
        UpdateSettings(role=PLANNER, settings=Settings(model="opus", effort="max")),
    ]


async def test_the_task_editor_offers_the_shown_roles_settings() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    shell.frontend.edit_task(None)
    [(_, choices, _)] = shell.app.editors
    assert choices.settings == Settings(model="haiku")


async def test_a_role_no_longer_listed_loses_its_tab_and_the_prompt_the_role() -> None:
    shell = await talking()
    await shell.click_tab(ARCHITECT)
    await shell.feed(BUILT_IN)
    assert shell.tab_strip == STRIP
    assert shell.active_tab == "All"
    assert shell.marks.placeholder == TALK_TO_PLANNER
    await shell.submit("plan")
    assert shell.wire.changes == [SendMessage(agent=PLANNER, text="plan")]


AUDITOR = AgentEntry(name="auditor", label="auditor", conversational=False, lane="security")
"""A role in lane security in workspace A, which B runs in lane audit."""


def two_workspaces() -> Catalog:
    """A catalog serving B, which this frontend opens on as it's listed first, configuring the
    auditor in lane audit, then A, which also configures the architect, the auditor in lane
    security; its own agents merge them, A's lanes first."""
    in_a = (PLANNER_ENTRY, ARCHITECT_ENTRY, AUDITOR)
    in_b = (PLANNER_ENTRY, AUDITOR.model_copy(update={"lane": "audit"}))
    a = WorkspaceEntry(id="A", label="a", sessions=(CURRENT,), agents=in_a)
    b = WorkspaceEntry(id="B", label="b", sessions=(CURRENT,), agents=in_b)
    return Catalog(workspaces=(b, a), agents=in_a, triggers=(), lanes=())


async def test_a_frontend_pins_and_groups_the_roles_of_the_workspace_it_is_on() -> None:
    shell = Shell()
    await shell.feed(two_workspaces())
    assert shell.frontend.navigation.place.workspace == "B"
    assert shell.tab_strip == STRIP
    assert shell.tasks.lanes == {PLANNER: "planner", "auditor": "audit"}


async def test_moving_to_another_workspace_pins_and_groups_its_roles() -> None:
    shell = Shell()
    await shell.feed(two_workspaces())
    assert await shell.frontend.navigation.place.join("A")
    shell.frontend.router.show_viewing()
    assert shell.tab_strip == [*STRIP, ARCHITECT]
    assert shell.tasks.lanes == {PLANNER: "planner", ARCHITECT: DESIGN, "auditor": "security"}


async def test_before_a_workspace_is_open_only_the_planner_is_seated() -> None:
    shell = Shell(linked=True)
    await shell.feed(two_workspaces())
    assert shell.frontend.navigation.place.workspace is None
    assert shell.frontend.router.seats.listed == (PLANNER_SEAT,)
    assert shell.tab_strip == STRIP
