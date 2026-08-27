"""Common fixtures and helpers for the SolarEdge tests.

The ``mock_modbus_unit`` fixture is provided by ``modbus-connection``'s pytest
plugin (registered via entry point), so tests only need to seed its ``holding``
store and drive the client.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings
from modbus_connection.mock import MockModbusConnection
from modbus_connection.model.sunspec import SunSpecModel

from solaredged.const import (
    COMMON_MODEL_ID,
    METER_DIDS,
    METER_MODEL_OFFSET,
    METER_SLOT_BASES,
    SUNSPEC_ID,
    SunSpecDID,
)

if TYPE_CHECKING:
    from modbus_connection.mock import MockModbusUnit

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Hypothesis profiles: a quick local default, a deeper sweep for CI. Select with
# the HYPOTHESIS_PROFILE env var (CI sets it to "ci").
settings.register_profile("dev", max_examples=100)
settings.register_profile(
    "ci", max_examples=1000, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))


def load_registers(name: str) -> dict[int, int]:
    """Load a captured holding-register dump keyed by address."""
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return {int(address): value for address, value in data["holding"].items()}


# The captured dumps stop short of the model-length registers, so they cannot
# drive a chain walk on their own. A real device serves the chain; these lay it
# down over the capture, sized by the models the capture shows.
_COMMON_LENGTH = 65
_INVERTER_LENGTH = 50
_METER_LENGTH = 105
_MMPPT_LENGTH = 48
_END_MARKER = 0xFFFF


def add_chain(unit: MockModbusUnit, *, mmppt: bool = False) -> None:
    """Lay a well-formed SunSpec chain over whatever the fixture already holds."""
    unit.holding[40000] = SUNSPEC_ID >> 16
    unit.holding[40001] = SUNSPEC_ID & 0xFFFF

    inverter_did = unit.holding.get(40069) or int(SunSpecDID.THREE_PHASE_INVERTER)
    inverter_length = _INVERTER_LENGTH

    unit.holding[40002] = COMMON_MODEL_ID
    unit.holding[40003] = _COMMON_LENGTH
    unit.holding[40069] = inverter_did
    unit.holding[40070] = inverter_length

    address = 40071 + inverter_length
    if mmppt:
        unit.holding[address] = int(SunSpecDID.MULTIPLE_MPPT)
        unit.holding[address + 1] = _MMPPT_LENGTH
        address += 2 + _MMPPT_LENGTH

    # Each meter the capture shows: a common block then its model block.
    for base in METER_SLOT_BASES:
        did = unit.holding.get(base + METER_MODEL_OFFSET)
        if did not in METER_DIDS:
            break
        unit.holding[base] = COMMON_MODEL_ID
        unit.holding[base + 1] = _COMMON_LENGTH
        unit.holding[base + METER_MODEL_OFFSET + 1] = _METER_LENGTH
        address = base + METER_MODEL_OFFSET + 2 + _METER_LENGTH

    unit.holding[address] = _END_MARKER


def seed(unit: MockModbusUnit, name: str, *, mmppt: bool = False) -> None:
    """Seed a mock unit's holding store from a captured fixture."""
    unit.holding.update(load_registers(name))
    add_chain(unit, mmppt=mmppt)


@pytest.fixture
def se17k_connection() -> MockModbusConnection:
    """Return a mock connection seeded with a real SE17K inverter register dump."""
    connection = MockModbusConnection()
    unit = connection.for_unit(1)
    unit.holding.update(load_registers("se17k_3phase.json"))
    add_chain(unit)
    return connection


def chain_models(
    *, mmppt: bool = False, meters: int = 0
) -> dict[int, list[SunSpecModel]]:
    """Build the model chain a device with this layout would serve.

    The counterpart to :func:`add_chain` for tests that construct a client
    directly rather than probing one.
    """
    models: dict[int, list[SunSpecModel]] = {
        COMMON_MODEL_ID: [SunSpecModel(COMMON_MODEL_ID, 40002, _COMMON_LENGTH)],
        int(SunSpecDID.THREE_PHASE_INVERTER): [
            SunSpecModel(int(SunSpecDID.THREE_PHASE_INVERTER), 40069, _INVERTER_LENGTH)
        ],
    }

    shift = 0
    if mmppt:
        models[int(SunSpecDID.MULTIPLE_MPPT)] = [
            SunSpecModel(int(SunSpecDID.MULTIPLE_MPPT), 40121, _MMPPT_LENGTH)
        ]
        shift = 2 + _MMPPT_LENGTH

    meter_did = int(SunSpecDID.THREE_PHASE_WYE_METER)
    for index in range(meters):
        base = METER_SLOT_BASES[index] + shift
        models[COMMON_MODEL_ID].append(
            SunSpecModel(COMMON_MODEL_ID, base, _COMMON_LENGTH)
        )
        models.setdefault(meter_did, []).append(
            SunSpecModel(meter_did, base + METER_MODEL_OFFSET, _METER_LENGTH)
        )

    return models
