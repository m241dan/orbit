"""The machine line a configurator is told: how many CPUs and how much memory the machine has."""

import pytest

from orbit.agent.domain.machine import Bytes, Machine, describe_machine

ADVICE = "Set each build and test step's parallelism to fit it."


@pytest.mark.parametrize(
    ("machine", "line"),
    [
        (
            Machine(cpus=4, memory=Bytes(8_160_000_000)),
            f"This machine: 4 CPUs, 7.6 GiB of memory. {ADVICE}",
        ),
        (Machine(cpus=4), f"This machine: 4 CPUs. {ADVICE}"),
        (Machine(cpus=1), f"This machine: 1 CPU. {ADVICE}"),
        (Machine(memory=Bytes(8 * 2**30)), f"This machine: 8.0 GiB of memory. {ADVICE}"),
        (Machine(), ""),
    ],
)
def test_the_machine_line_names_what_is_known(machine: Machine, line: str) -> None:
    assert describe_machine(machine) == line
