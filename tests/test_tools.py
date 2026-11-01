from orbit.agent.domain.tools import DRAFT_TOOLS, ORBIT_TOOLS, OrbitToolGrant, orbit_tool_grant


def test_orbit_tools_are_the_eighteen_in_order() -> None:
    assert ORBIT_TOOLS == (
        "get_task",
        "complete_task",
        "run_check",
        "report_progress",
        "give_up",
        "suggest_task",
        "propose_task",
        "propose_checks",
        "consult",
        "save_report",
        "list_reports",
        "read_report",
        "list_drafts",
        "read_draft",
        "edit_draft",
        "remove_draft",
        "move_draft",
        "read_skill",
    )


def test_draft_tools_are_the_five_in_order() -> None:
    assert DRAFT_TOOLS == ("list_drafts", "read_draft", "edit_draft", "remove_draft", "move_draft")


def test_an_orbit_tool_grant_gives_the_roles_tools_and_withholds_orbits_others() -> None:
    grant = orbit_tool_grant(("complete_task", "get_task"))
    assert grant.granted == ("complete_task", "get_task")
    assert grant.withheld == (
        "run_check",
        "report_progress",
        "give_up",
        "suggest_task",
        "propose_task",
        "propose_checks",
        "consult",
        "save_report",
        "list_reports",
        "read_report",
        "list_drafts",
        "read_draft",
        "edit_draft",
        "remove_draft",
        "move_draft",
        "read_skill",
    )


def test_an_orbit_tool_grant_of_every_tool_withholds_none() -> None:
    assert orbit_tool_grant(ORBIT_TOOLS) == OrbitToolGrant(ORBIT_TOOLS, ())


def test_an_orbit_tool_grant_of_no_tools_withholds_every_one() -> None:
    assert orbit_tool_grant(()) == OrbitToolGrant((), ORBIT_TOOLS)
