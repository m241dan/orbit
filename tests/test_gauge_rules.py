"""The rules of the control plane's live gauges: how many agents run in each role in each
workspace served."""

from orbit.agent.domain.gauges import agents_by_role


def test_each_role_a_workspace_configures_counts_its_agents_running_there() -> None:
    roles = {"a": ("coder", "reviewer"), "b": ("coder",)}
    running = [("a", "coder"), ("a", "coder"), ("b", "coder")]

    assert agents_by_role(roles, running) == {
        ("a", "coder"): 2,
        ("a", "reviewer"): 0,
        ("b", "coder"): 1,
    }


def test_a_role_the_workspace_doesnt_configure_counts_while_its_agents_run() -> None:
    assert agents_by_role({"a": ("coder",)}, [("a", "checker")]) == {
        ("a", "coder"): 0,
        ("a", "checker"): 1,
    }


def test_the_agents_of_a_workspace_not_served_dont_count() -> None:
    assert agents_by_role({"a": ("coder",)}, [("gone", "coder")]) == {("a", "coder"): 0}
