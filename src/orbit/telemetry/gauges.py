"""The gauge port over OpenTelemetry's observable gauges, each read as metrics are collected."""

import logging
from collections.abc import Callable, Iterable

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from orbit.tracing import Reading

logger = logging.getLogger(__name__)

Read = Callable[[], Iterable[Reading]]
"""What gives a gauge's values, each with its attributes."""


class OtelGauges:
    """Gauges registered as observable gauges with ``meter``, each read at every collection."""

    def __init__(self, meter: Meter) -> None:
        self._meter = meter

    def register(self, name: str, unit: str, description: str, read: Read) -> None:
        """Have the gauge ``name``, measured in ``unit`` and described by ``description``,
        report what ``read`` gives at each collection."""
        self._meter.create_observable_gauge(
            name, callbacks=[_Observed(name, read)], unit=unit, description=description
        )


class _Observed:
    """The observations of the gauge ``name``: what ``read`` gives, or none when it raises,
    which is warned of once, until it reads again."""

    def __init__(self, name: str, read: Read) -> None:
        self._name = name
        self._read = read
        self._failing = False

    def __call__(self, options: CallbackOptions) -> list[Observation]:
        """What ``read`` gives as observations; none if it raises."""
        # A gauge reads state another thread changes, so a read can fail; the other gauges
        # still report, and a gauge failing at every collection isn't warned of every time.
        try:
            observations = [Observation(value, attributes) for value, attributes in self._read()]
        except Exception as error:
            if not self._failing:
                logger.warning("Couldn't read the gauge %s: %s", self._name, error)
            self._failing = True
            return []
        self._failing = False
        return observations
