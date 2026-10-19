"""Whether this orbit owns the repository, shared by every part that acts differently while
another orbit owns it."""


class Ownership:
    """Whether this orbit is ``read_only``, as another orbit owns the repository, until it
    takes the repository over with :meth:`own`."""

    def __init__(self, read_only: bool = False) -> None:
        self._read_only = read_only

    @property
    def read_only(self) -> bool:
        """Whether another orbit owns the repository, so work is queued and reviews wait."""
        return self._read_only

    def own(self) -> None:
        """Own the repository from now on."""
        self._read_only = False
