"""What a session spent per role, and what each role caused others to spend by consulting them:
a consulted turn counts for its consultant and, beside, for the role that asked."""

from dataclasses import dataclass, field

from orbit.protocol import Usage


@dataclass(frozen=True)
class RoleSpend:
    """What each role spent, ``by_role``, and what each asking role caused each of its
    consultants to spend, ``caused``, by asker then consultant."""

    by_role: dict[str, Usage] = field(default_factory=dict)
    caused: dict[str, dict[str, Usage]] = field(default_factory=dict)

    def charged(self, role: str, usage: Usage, asker: str | None = None) -> RoleSpend:
        """This spend with ``usage``, a turn of ``role``'s, added under it and, if the turn
        answered a consultation of the role ``asker``, under what ``asker`` caused it to
        spend."""
        by_role = _added(self.by_role, role, usage)
        if asker is None:
            return RoleSpend(by_role, self.caused)
        caused = {**self.caused, asker: _added(self.caused.get(asker, {}), role, usage)}
        return RoleSpend(by_role, caused)


def _added(spent: dict[str, Usage], role: str, usage: Usage) -> dict[str, Usage]:
    """``spent`` with ``usage`` added under ``role``."""
    return {**spent, role: spent.get(role, Usage()) + usage}
