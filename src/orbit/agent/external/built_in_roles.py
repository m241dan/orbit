"""The built-in roles, told the instructions shipped with orbit in ``instructions/<role>.md``."""

from importlib.resources import files

from orbit.agent.domain.roles import Role, built_in_roles
from orbit.protocol import BUILT_IN_ROLE_NAMES


def read_built_in_roles() -> dict[str, Role]:
    """The built-in roles, each told the text of its Markdown file."""
    folder = files(__package__) / "instructions"
    return built_in_roles(
        {
            name: (folder / f"{name}.md").read_text(encoding="utf-8").strip()
            for name in BUILT_IN_ROLE_NAMES
        }
    )
