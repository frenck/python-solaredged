"""Property-based fuzz tests for the register decoders.

The device is untrusted input: a faulty or hostile inverter can put any 16-bit
value in any register. These tests throw random register soup at the decoders
and assert two properties:

* the client never raises anything other than its own ``SolarEdgeError`` /
  ``SolarEdgeConnectionError`` (never a raw ``ValueError``, ``struct.error``,
  ``OverflowError``, ``KeyError`` and so on), and
* every decoded value is either ``None`` or a sane, finite, correctly typed
  value (no ``NaN``/``inf`` leaking through, no wrong types).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from enum import Enum

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from modbus_connection.mock import MockModbusConnection

from solaredged import SolarEdge, SunSpecDID
from solaredged.exceptions import SolarEdgeError

from .conftest import load_registers

_FIXTURE = "se17k_3phase.json"
_BASE = load_registers(_FIXTURE)

# A valid inverter DID (register 40069) so the always-present-inverter guard in
# async_update does not short-circuit the decode fuzz before it asserts.
_INVERTER_DID = int(SunSpecDID.THREE_PHASE_INVERTER)

# One 16-bit register word.
_WORD = st.integers(min_value=0, max_value=0xFFFF)
# Addresses across every block the library reads: inverter/mmppt/meters,
# storage/export control and batteries, and power/advanced power control.
_ADDRESS = st.one_of(
    st.integers(min_value=40000, max_value=40700),
    st.integers(min_value=57340, max_value=58600),
    st.integers(min_value=61440, max_value=61900),
)
_REGISTERS = st.dictionaries(keys=_ADDRESS, values=_WORD, max_size=300)

# Example count comes from the active Hypothesis profile (see conftest); only
# the deadline is overridden here (the mock has no real latency).
_SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _assert_well_typed(client: SolarEdge) -> None:
    """Every decoded register value must be None or a finite, sane, typed value."""
    components = list(client.components)
    if client.mmppt is not None:
        components.extend(client.mmppt.modules)

    for component in components:
        for name in type(component)._register_fields:
            value = getattr(component, name)
            assert value is None or isinstance(value, (int, float, str, Enum)), (
                f"{type(component).__name__}.{name} decoded to {value!r}"
            )
            if isinstance(value, float):
                assert math.isfinite(value), f"{name} decoded to non-finite {value!r}"


@_SETTINGS
@given(registers=_REGISTERS)
def test_probe_survives_random_registers(registers: dict[int, int]) -> None:
    """Probing a device full of random registers never crashes unexpectedly."""

    async def _run() -> None:
        connection = MockModbusConnection()
        unit = connection.for_unit(1)
        unit.holding.update(_BASE)  # a plausible device as the starting point
        unit.holding.update(registers)  # then random noise on top
        unit.holding[40069] = _INVERTER_DID  # keep the inverter identity valid

        try:
            client = await SolarEdge.async_probe(unit)
            await client.async_update()
        except SolarEdgeError:  # SolarEdgeConnectionError is a subclass
            return  # rejecting a garbled device is a valid, documented outcome

        _assert_well_typed(client)

    asyncio.run(_run())


@_SETTINGS
@given(registers=_REGISTERS)
def test_decode_survives_random_registers(registers: dict[int, int]) -> None:
    """Decoding a full device layout from random registers never crashes.

    Builds every component directly (so meter/battery/control decoders are all
    exercised) and reads pure noise. The MMPPT module count is pinned to a safe
    value; via probe it is validated to 2-3, but constructed directly the raw
    count would otherwise drive an unbounded repeating-group read.
    """

    async def _run() -> None:
        connection = MockModbusConnection()
        unit = connection.for_unit(1)
        unit.holding.update(registers)
        unit.holding[40069] = _INVERTER_DID  # keep the inverter identity valid
        unit.holding[40129] = 2  # bound the MMPPT module count

        client = SolarEdge(
            unit,
            meters=3,
            batteries=3,
            mmppt=True,
            storage_control=True,
            export_control=True,
            power_control=True,
            advanced_power_control=True,
        )
        try:
            await client.async_update()
        except SolarEdgeError:
            return  # undecodable garbage surfaces as our own error, not a crash

        _assert_well_typed(client)

    asyncio.run(_run())


@_SETTINGS
@given(value=st.integers())
def test_write_int_field_never_crashes_raw(value: int) -> None:
    """Writing any integer to a register field raises only SolarEdgeError.

    An out-of-range value must not leak a raw OverflowError from the encoder.
    """

    async def _run() -> None:
        connection = MockModbusConnection()
        client = SolarEdge(connection.for_unit(1), power_control=True)
        assert client.power_control is not None
        with contextlib.suppress(SolarEdgeError):
            await client.power_control.write("active_power_limit", value)

    asyncio.run(_run())


@_SETTINGS
@given(value=st.floats())  # includes NaN, +/-inf, and out-of-range magnitudes
def test_write_float_field_never_crashes_raw(value: float) -> None:
    """Writing any float via the register or direct-encode paths never crashes raw."""

    async def _run() -> None:
        connection = MockModbusConnection()
        client = SolarEdge(
            connection.for_unit(1),
            storage_control=True,
            advanced_power_control=True,
        )
        storage = client.storage_control
        advanced = client.advanced_power_control
        assert storage is not None
        assert advanced is not None

        for write in (
            lambda: storage.write("backup_reserve", value),
            lambda: storage.write("charge_limit", value),
            lambda: advanced.set_current_limit(value),
            lambda: advanced.set_reactive_power(value),
        ):
            with contextlib.suppress(SolarEdgeError):
                await write()

    asyncio.run(_run())
