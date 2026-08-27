"""Navigating a discovered SunSpec model chain.

``scan()`` on modbus-connection 3.x returns a plain mapping of model id to the
models found with it. These are the two lookups this library needs; the 4.x
line offers them on the returned object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modbus_connection.model.sunspec import SunSpecModel

# A model occupies its two header registers plus its declared length.
_HEADER = 2


def span(model: SunSpecModel) -> int:
    """Return the registers the whole model occupies, header included."""
    return _HEADER + model.length


def first(
    models: dict[int, list[SunSpecModel]], *model_ids: int
) -> SunSpecModel | None:
    """Return the first model among *model_ids*, tried in argument order."""
    for model_id in model_ids:
        found = models.get(model_id)
        if found:
            return found[0]
    return None


def at(models: dict[int, list[SunSpecModel]], address: int) -> SunSpecModel | None:
    """Return the model starting at *address*, if the chain has one there."""
    for found in models.values():
        for model in found:
            if model.address == address:
                return model
    return None
