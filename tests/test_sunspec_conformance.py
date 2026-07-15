"""Verify the register map matches the SunSpec model definitions.

The inverter and meter blocks follow the standard SunSpec information model, so
their field addresses, sign, size and scale-factor wiring can be checked against
the authoritative model definitions (vendored under ``fixtures/sunspec``, from
the Apache-2.0 ``sunspec/models`` project). This catches an off-by-one address,
a wrong sign, or a misrouted scale register independently of any device dump.

Two independent oracles resolve those definitions. One parses the model JSON
here; the other runs ``modbus-connection``'s own official-model generator and
reads back the fields it produces. Agreement between the two means our hand
declarations match both the raw spec and how the framework itself would model
it, so a regenerated map would land on the same layout.

The SolarEdge proprietary blocks (battery, storage, export, power control) are
not part of SunSpec and are not checked here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from modbus_connection.model.sunspec import SunSpecComponent
from modbus_connection.model.sunspec.generate import generate_source

from solaredged.components import Common, InverterExtended, Meter

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


def _generated_index(model_file: str, base: int) -> dict[int, _Point]:
    """Index a model's points as ``modbus-connection``'s generator resolves them.

    The second oracle: rather than parse the JSON ourselves, run the
    official-model generator and read back the fields it emits. Generated
    addresses are model-relative (the model starts at zero), so they shift by
    ``base`` to the absolute address our fields declare.
    """
    model = json.loads((_SPEC_DIR / model_file).read_text())

    # Give the generated module a stable identity before executing it.
    namespace: dict[str, object] = {"__name__": model_file}
    exec(compile(generate_source([model]), model_file, "exec"), namespace)  # noqa: S102

    component = next(
        (
            obj
            for obj in namespace.values()
            if isinstance(obj, type)
            and issubclass(obj, SunSpecComponent)
            and obj is not SunSpecComponent
        ),
        None,
    )
    assert component is not None, f"generator produced no component for {model_file}"

    index: dict[int, _Point] = {}
    for name, field in component._register_fields.items():
        scale = (
            base + field.scale_register if field.scale_register is not None else None
        )
        index[base + field.address] = _Point(
            name=name,
            signed=getattr(field, "signed", False),
            size=field.count,
            scale_register=scale,
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
    _assert_conforms(Common, _generated_index("model_1.json", 40002))


def test_inverter_block_matches_sunspec_model_103() -> None:
    """The inverter measurement block conforms to SunSpec model 103.

    Checked through InverterExtended so the standard points and the SolarEdge
    extension are covered in one pass; the extension's own points are
    proprietary reuses of registers in this block, not standard SunSpec.
    """
    skip = frozenset({"_grid_status", "vendor_status_extended"})

    _assert_conforms(InverterExtended, _spec_index("model_103.json", 40069), skip=skip)
    _assert_conforms(
        InverterExtended, _generated_index("model_103.json", 40069), skip=skip
    )


def test_meter_block_matches_sunspec() -> None:
    """The meter identity and measurement blocks conform to SunSpec models 1 + 203."""
    _assert_conforms(
        Meter, _spec_index("model_1.json", 40121) | _spec_index("model_203.json", 40188)
    )
    _assert_conforms(
        Meter,
        _generated_index("model_1.json", 40121)
        | _generated_index("model_203.json", 40188),
    )
