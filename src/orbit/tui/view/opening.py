"""How the app opens, once the backend first lists its workspaces."""

from collections.abc import Sequence

from orbit.protocol import Catalog, Event


def lists_catalog(batch: Sequence[Event]) -> bool:
    """Whether ``batch`` holds a catalog."""
    return any(isinstance(event, Catalog) for event in batch)
