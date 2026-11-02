"""Credentials from orbit's own environment: the values MCP servers' environments are filled
from."""

import os
from collections.abc import Mapping


class EnvironmentCredentials:
    """Looks variables up in ``environ``, orbit's environment unless given, as it stands at each
    lookup."""

    def __init__(self, environ: Mapping[str, str] = os.environ) -> None:
        self._environ = environ

    def __call__(self, name: str) -> str | None:
        """The value of the variable ``name``; ``None`` if it isn't set."""
        return self._environ.get(name)
