"""Credentials looked up in layers: orbit's environment first, then ``credentials.toml``."""

from dataclasses import dataclass

from orbit.agent.application.ports import Credentials, CredentialsUnreadable
from orbit.agent.domain.credentials import unset_because


@dataclass(frozen=True)
class LayeredCredentials:
    """Credentials looked up in ``first``, and in ``fallback`` for a variable ``first`` doesn't
    set, so that ``first`` wins; ``fallback`` is read only when it's needed."""

    first: Credentials
    fallback: Credentials

    def __call__(self, name: str) -> str | None:
        """The value of the variable ``name`` in ``first``, else in ``fallback``; ``None`` if
        neither sets it. Raises :class:`CredentialsUnreadable`, saying the variable isn't set
        and why, if ``first`` doesn't set it and ``fallback`` can't be read."""
        value = self.first(name)
        if value is not None:
            return value
        try:
            return self.fallback(name)
        except CredentialsUnreadable as exc:
            raise CredentialsUnreadable(unset_because(name, str(exc))) from None
