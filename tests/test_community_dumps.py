"""Decode real device register maps and check the result.

Each fixture under ``fixtures/community`` is a raw holding-register map captured
from a Home Assistant diagnostics dump attached to a GitHub issue. Together they
exercise the decoder against a spread of real devices: single, split and
three-phase inverters, meters, batteries, and an EV charger.

Every decode is pinned two ways: a snapshot guards against value drift, and a
set of decoder-agnostic physical-plausibility checks (line voltage, frequency,
state of charge) catch anything that decodes to a nonsensical magnitude.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from solaredged import SolarEdge

from .conftest import FIXTURES_DIR, load_registers

if TYPE_CHECKING:
    from modbus_connection.mock import MockModbusUnit
    from syrupy.assertion import SnapshotAssertion

    from solaredged.components import Component

# The EV charger is exercised on its own; every other capture is a real device.
_EV_CHARGER = "issue565_u3.json"
_DEVICES = sorted(
    path.name
    for path in (FIXTURES_DIR / "community").glob("*.json")
    if path.name != _EV_CHARGER
)


def _decoded(component: Component) -> dict[str, object]:
    """Every decoded register field of a component, by name."""
    names = sorted(type(component)._register_fields)
    return {name: getattr(component, name) for name in names}


def _summary(client: SolarEdge) -> dict[str, object]:
    """Return the decoded inverter identity, measurements, meters and batteries."""
    return {
        "common": _decoded(client.common),
        "inverter": _decoded(client.inverter),
        "meters": [_decoded(meter) for meter in client.meters],
        "batteries": [_decoded(battery) for battery in client.batteries],
    }


def test_device_corpus_present() -> None:
    """Guard against the fixture set silently going empty."""
    assert len(_DEVICES) >= 8


@pytest.mark.parametrize("fixture", _DEVICES)
async def test_decode_real_device(
    fixture: str,
    mock_modbus_unit: MockModbusUnit,
    snapshot: SnapshotAssertion,
) -> None:
    """A real device register map decodes to stable, plausible values."""
    mock_modbus_unit.holding.update(load_registers(f"community/{fixture}"))

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    inverter = client.inverter
    assert inverter.did is not None  # a real inverter always identifies itself

    # Decoder-agnostic sanity: these magnitudes must be physically plausible.
    if inverter.ac_voltage_an is not None:
        assert 100 < inverter.ac_voltage_an < 500
    if inverter.ac_frequency is not None:
        assert 45 < inverter.ac_frequency < 65
    for meter in client.meters:
        if meter.ac_frequency is not None:
            assert 45 < meter.ac_frequency < 65
    for battery in client.batteries:
        if battery.state_of_energy is not None:
            assert 0 <= battery.state_of_energy <= 110

    # Regression: the full decoded device must not drift.
    assert _summary(client) == snapshot


async def test_ev_charger_decodes_gracefully(mock_modbus_unit: MockModbusUnit) -> None:
    """A SolarEdge EV charger presents as an inverter but exposes no telemetry.

    It answers on an inverter DID with a normal common block, but every
    measurement register is a not-implemented sentinel. Those must decode to
    None, and the unit must be recognisable as a charger, not a producing
    inverter.
    """
    mock_modbus_unit.holding.update(load_registers(f"community/{_EV_CHARGER}"))

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    assert client.inverter.ac_power is None
    assert client.inverter.ac_energy is None
    assert client.is_ev_charger is True
