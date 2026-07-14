"""Verify the register map matches the SunSpec model definitions.

The inverter and meter blocks follow the standard SunSpec information model, so
their field addresses, sign, size and scale-factor wiring can be checked against
the authoritative model definitions (vendored under ``fixtures/sunspec``, from
the Apache-2.0 ``sunspec/models`` project). This catches an off-by-one address,
a wrong sign, or a misrouted scale register independently of any device dump.

The SolarEdge proprietary blocks (battery, storage, export, power control) are
not part of SunSpec and are not checked here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from solaredged.components import Common, Inverter, Meter

if TYPE_CHECKING:
    from modbus_connection.model import Component

_SPEC_DIR = Path(__file__).parent / "fixtures" / "sunspec"

# SunSpec point types that map to a signed register value.
_SIGNED_TYPES = {"int16", "int32", "int64"}


class _Point(NamedTuple):
    """A SunSpec model point resolved to what our fields declare."""

    name: str
    signed: bool
    size: int  # registers
    scale_register: int | None  # absolute address of the sunssf point, if any


def _spec_index(model_file: str, base: int) -> dict[int, _Point]:
    """Index a SunSpec model's points by their absolute register address.

    ``base`` is the address of the model's first point; each following point sits
    at the running sum of the preceding point sizes.
    """
    points = json.loads((_SPEC_DIR / model_file).read_text())["group"]["points"]

    # Resolve every point's absolute address first, so scale-factor references
    # (which point at another point by name) can be turned into addresses.
    address: dict[str, int] = {}
    offset = 0
    for point in points:
        address[point["name"]] = base + offset
        offset += point["size"]

    index: dict[int, _Point] = {}
    for point in points:
        scale_register = address[point["sf"]] if point.get("sf") else None
        index[address[point["name"]]] = _Point(
            name=point["name"],
            signed=point["type"] in _SIGNED_TYPES,
            size=point["size"],
            scale_register=scale_register,
        )
    return index


def _assert_conforms(
    component: type[Component],
    spec: dict[int, _Point],
    *,
    skip: frozenset[str] = frozenset(),
) -> None:
    """Assert every register field of a component matches its SunSpec point."""
    for name, field in component._register_fields.items():
        if name in skip:
            continue

        point = spec.get(field.address)
        assert point is not None, f"{name} @ {field.address} is not a SunSpec point"
        assert field.count == point.size, (
            f"{name}: size {field.count} registers, spec {point.size}"
        )
        assert field.scale_register == point.scale_register, (
            f"{name}: scale reg {field.scale_register}, spec {point.scale_register}"
        )

        # String fields have no sign; only number fields carry the flag.
        if hasattr(field, "signed"):
            assert field.signed == point.signed, (
                f"{name}: signed {field.signed}, spec {point.signed} ({point.name})"
            )


def test_common_block_matches_sunspec_model_1() -> None:
    """The inverter identity block conforms to the SunSpec common model."""
    # The "SunS" marker occupies 40000-40001, so the common model starts at 40002.
    _assert_conforms(Common, _spec_index("model_1.json", 40002))


def test_inverter_block_matches_sunspec_model_103() -> None:
    """The inverter measurement block conforms to SunSpec model 103."""
    spec = _spec_index("model_103.json", 40069)

    # _grid_status and vendor_status_extended are SolarEdge proprietary points
    # that reuse registers in this block; they are not standard SunSpec.
    _assert_conforms(
        Inverter, spec, skip=frozenset({"_grid_status", "vendor_status_extended"})
    )


def test_meter_block_matches_sunspec() -> None:
    """The meter identity and measurement blocks conform to SunSpec models 1 + 203."""
    spec = _spec_index("model_1.json", 40121) | _spec_index("model_203.json", 40188)
    _assert_conforms(Meter, spec)
