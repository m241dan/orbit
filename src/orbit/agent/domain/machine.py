"""The machine orbit runs on, as a configurator is told it: how many CPUs the process may use
and how much memory there is, either unknown where it can't be read."""

from dataclasses import dataclass
from typing import NewType

Bytes = NewType("Bytes", int)
"""A size in bytes."""

GIB = 2**30
"""The bytes in a GiB."""

ADVICE = "Set each build and test step's parallelism to fit it."
"""What the machine line asks of a configurator."""


@dataclass(frozen=True)
class Machine:
    """The machine's size: the ``cpus`` the process may use and its physical ``memory``, each
    none when it couldn't be read."""

    cpus: int | None = None
    memory: Bytes | None = None


def describe_machine(machine: Machine) -> str:
    """The line telling a configurator ``machine``'s size and to fit parallelism to it, naming
    what's known of it; empty when nothing is."""
    facts = tuple(fact for fact in (_cpus(machine.cpus), _memory(machine.memory)) if fact)
    return f"This machine: {', '.join(facts)}. {ADVICE}" if facts else ""


def _cpus(cpus: int | None) -> str:
    """The count of ``cpus``, as ``4 CPUs`` or ``1 CPU``; empty when unknown."""
    if cpus is None:
        return ""
    return f"{cpus} CPU" if cpus == 1 else f"{cpus} CPUs"


def _memory(memory: Bytes | None) -> str:
    """``memory`` in GiB to one decimal, as ``7.6 GiB of memory``; empty when unknown."""
    return "" if memory is None else f"{memory / GIB:.1f} GiB of memory"
