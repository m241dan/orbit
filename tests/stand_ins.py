"""Stand-ins for the executables of the runtimes agents run on."""

from pathlib import Path

STAND_IN = "#!/bin/sh\nexit 1\n"
"""An executable that does nothing but fail, standing in for a runtime's."""


def stand_in(directory: Path, name: str) -> Path:
    """A stand-in executable ``name`` in ``directory``, made with it if it's missing."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(STAND_IN)
    path.chmod(0o755)
    return path
