"""The ownership the session's parts share: read-only while another orbit owns the repository."""

from orbit.agent.application.ownership import Ownership


def test_ownership_is_owned_by_default() -> None:
    assert not Ownership().read_only


def test_ownership_given_another_owner_starts_read_only() -> None:
    assert Ownership(read_only=True).read_only


def test_owning_the_repository_ends_read_only() -> None:
    ownership = Ownership(read_only=True)
    ownership.own()
    assert not ownership.read_only
