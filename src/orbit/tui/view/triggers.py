"""The keys that fire triggers, and the labels the footer shows for them."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from orbit.protocol import CONFIGURE_CHECKS, SUPER_REVIEW, TriggerEntry

TRIGGER_KEYS: Mapping[str, str] = MappingProxyType(
    {"ctrl+g": SUPER_REVIEW, "ctrl+e": CONFIGURE_CHECKS}
)
"""The trigger each key fires, by name."""


def trigger_key_labels(
    triggers: Iterable[TriggerEntry], keys: Mapping[str, str] = TRIGGER_KEYS
) -> dict[str, str | None]:
    """Each of ``keys``' footer label: its trigger's among ``triggers``, or none for a key whose
    trigger isn't there."""
    labels = {trigger.name: trigger.label for trigger in triggers}
    return {key: labels.get(trigger) for key, trigger in keys.items()}
