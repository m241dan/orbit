"""A workspace host's display name: its workspace's path, or its id without one."""

from orbit.agent import WorkspaceHost


def test_a_host_with_a_path_is_named_by_it() -> None:
    assert WorkspaceHost(workspace="/work", workspace_id="w").display_name == "/work"


def test_a_host_without_a_path_is_named_by_its_id() -> None:
    assert WorkspaceHost(workspace_id="w").display_name == "w"
