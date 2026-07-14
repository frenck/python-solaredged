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


def seed(unit: MockModbusUnit, name: str) -> None:
    """Seed a mock unit's holding store from a captured fixture."""
    unit.holding.update(load_registers(name))


@pytest.fixture
def se17k_connection() -> MockModbusConnection:
    """Return a mock connection seeded with a real SE17K inverter register dump."""
    connection = MockModbusConnection()
    connection.for_unit(1).holding.update(load_registers("se17k_3phase.json"))
    return connection
