"""Tests for the SolarEdge Modbus client."""

# pylint: disable=protected-access
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from modbus_connection import (
    ModbusConnectionError,
    ModbusExceptionError,
    ModbusTimeoutError,
)
from modbus_connection.decode import decode_float32
from modbus_connection.encode import encode_float32, encode_int
from syrupy.assertion import SnapshotAssertion

from solaredged import (
    BatteryStatus,
    ExportControlMode,
    InverterStatus,
    ReactivePowerConfig,
    SolarEdge,
    SolarEdgeConnectionError,
    SolarEdgeError,
    StorageChargePolicy,
    StorageControlMode,
    StorageMode,
    SunSpecDID,
)

from .conftest import seed

if TYPE_CHECKING:
    from collections.abc import Mapping

    from modbus_connection.mock import MockModbusConnection, MockModbusUnit

    from solaredged.components import Component

FIXTURE = "se17k_3phase.json"


def _dump(component: Component) -> dict[str, object]:
    """Every decoded register field of a component, by name."""
    names = sorted(component._register_fields)
    return {name: getattr(component, name) for name in names}


def _words(holding: Mapping[int, Any], *addresses: int) -> list[int]:
    """Read raw register words at the given addresses as a plain ``list[int]``.

    The mock's holding store has a loose value type; this narrows the read back
    to ``list[int]`` for encoders/decoders that expect it.
    """
    return [holding[address] for address in addresses]


def _seed_meter(unit: MockModbusUnit, index: int = 1, shift: int = 0) -> None:
    """Seed a synthetic three-phase meter at the given index (and MMPPT shift)."""
    base = shift + 174 * (index - 1)
    holding = unit.holding

    holding[40188 + base] = int(SunSpecDID.THREE_PHASE_WYE_METER)

    # Current and power, each with its own scale factor.
    holding[40190 + base] = 850  # AC current raw
    holding[40194 + base] = 0xFFFE  # AC current SF = -2 (so scaling is observable)
    holding[40206 + base] = encode_int(-800, count=1)[0]  # AC power raw
    holding[40210 + base] = 0  # AC power SF

    # Real energy accumulators (Wh), sharing one scale factor.
    holding[40226 + base] = encode_int(123000, count=2)  # exported
    holding[40234 + base] = encode_int(456000, count=2)  # imported
    holding[40242 + base] = 0  # energy SF


def _seed_battery(unit: MockModbusUnit, offset: int = 0) -> None:
    """Seed a synthetic battery at the given base offset."""
    holding = unit.holding
    holding[57666 + offset] = encode_float32(
        10000.0, word_order="little"
    )  # rated energy
    holding[57716 + offset] = encode_float32(-1200.0, word_order="little")  # dc power
    holding[57732 + offset] = encode_float32(
        87.5, word_order="little"
    )  # state of energy
    holding[57734 + offset] = encode_int(
        int(BatteryStatus.DISCHARGE), count=2, word_order="little"
    )


def _seed_mmppt(unit: MockModbusUnit, modules: int = 2) -> None:
    """Seed a synthetic multiple-MPPT block with the given module count."""
    holding = unit.holding

    # Header: identifier, then the shared scale factors and module count.
    holding[40121] = 160  # DID
    holding[40122] = 20  # length
    holding[40123] = 0xFFFE  # DCA_SF = -2
    holding[40124] = 0xFFFF  # DCV_SF = -1
    holding[40125] = 0  # DCW_SF
    holding[40126] = 0  # DCWH_SF
    holding[40129] = modules

    # One 20-register block per DC module.
    for module in range(modules):
        base = 40131 + module * 20
        holding[base] = module + 1  # module id
        holding[base + 9] = 850 - module  # DCA
        holding[base + 10] = 3900  # DCV
        holding[base + 11] = 3300  # DCW
        holding[base + 16] = 45  # Tmp
        holding[base + 17] = 4  # DCSt


# -- probing / detection -------------------------------------------------------


async def test_probe_requires_sunspec(mock_modbus_unit: MockModbusUnit) -> None:
    """A device without the SunSpec marker is rejected."""
    mock_modbus_unit.holding[40000] = [0x0000, 0x0000]
    with pytest.raises(SolarEdgeError, match="SunSpec"):
        await SolarEdge.async_probe(mock_modbus_unit)


async def test_probe_real_inverter(mock_modbus_unit: MockModbusUnit) -> None:
    """A real SE17K dump probes to an inverter with no meters or batteries."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.meters == []
    assert client.batteries == []
    assert client.storage_control is not None
    assert client.export_control is not None
    assert client.power_control is not None


class _PickyUnit:
    """Wraps a mock unit, raising a chosen error for a set of base addresses.

    By default it raises an illegal-data-address Modbus exception (an absent
    optional block); pass ``error`` to simulate a transport failure instead.
    The mock backend cannot express either on its own.
    """

    def __init__(
        self,
        inner: MockModbusUnit,
        fail_at: set[int],
        *,
        error: Exception | None = None,
    ) -> None:
        self._inner = inner
        self._fail_at = fail_at
        self._error = error or ModbusExceptionError(2)

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        if address in self._fail_at:
            raise self._error
        return await self._inner.read_holding_registers(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _TransientUnit:
    """Wraps a mock unit and fails the next read once, then behaves normally.

    Models a transient blip (a single timed-out read) so a subsequent retry can
    be shown to recover, rather than leaving the client wedged.
    """

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self._fail_next = False

    def fail_next_read(self) -> None:
        """Arm a single-read failure on the next ``read_holding_registers``."""
        self._fail_next = True

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        if self._fail_next:
            self._fail_next = False
            msg = "transient blip"
            raise ModbusTimeoutError(msg)
        return await self._inner.read_holding_registers(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_probe_handles_unreadable_blocks(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Optional blocks that reject a read are treated as absent."""
    seed(mock_modbus_unit, FIXTURE)
    unit = _PickyUnit(mock_modbus_unit, fail_at={40188, 57666, 57348, 57344, 61440})

    client = await SolarEdge.async_probe(unit)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert client.meters == []
    assert client.batteries == []
    assert client.storage_control is None
    assert client.export_control is None
    assert client.power_control is None


async def test_probe_transport_error_is_not_swallowed(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A transport failure mid-probe raises, rather than looking like absence."""
    seed(mock_modbus_unit, FIXTURE)
    # Header reads fine, but the meter probe drops the connection.
    unit = _PickyUnit(
        mock_modbus_unit, fail_at={40188}, error=ModbusConnectionError("link lost")
    )

    with pytest.raises(SolarEdgeConnectionError):
        await SolarEdge.async_probe(unit)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_probe_counts_meters_and_batteries(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Meters and batteries are counted from their identity registers."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_meter(mock_modbus_unit, index=1)
    _seed_meter(mock_modbus_unit, index=2)
    _seed_battery(mock_modbus_unit, offset=0)

    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert len(client.meters) == 2
    assert len(client.batteries) == 1


async def test_probe_counts_all_three_batteries(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Three batteries are counted, exercising the full detection loop."""
    seed(mock_modbus_unit, FIXTURE)
    for offset in (0, 256, 768):
        _seed_battery(mock_modbus_unit, offset=offset)

    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert len(client.batteries) == 3


async def test_mmppt_read_failure_treated_as_absent(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """An MMPPT header that rejects the read is treated as absent, not an error."""
    seed(mock_modbus_unit, FIXTURE)
    unit = _PickyUnit(mock_modbus_unit, fail_at={40121})  # MMPPT_BASE
    client = await SolarEdge.async_probe(unit)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert client.mmppt is None


# -- decoding ------------------------------------------------------------------


async def test_inverter_readings(
    mock_modbus_unit: MockModbusUnit, snapshot: SnapshotAssertion
) -> None:
    """The inverter block decodes to the expected typed values."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    manufacturer = client.common.manufacturer
    assert manufacturer is not None
    assert manufacturer.strip() == "SolarEdge"
    assert client.inverter.did is SunSpecDID.THREE_PHASE_INVERTER
    assert client.inverter.status is InverterStatus.PRODUCING
    # A three-phase inverter reports no cabinet temperature: unimplemented -> None.
    assert client.inverter.temperature_cabinet is None
    # Frequency is scaled by its own scale factor.
    assert client.inverter.ac_frequency == pytest.approx(50.0, abs=1.0)

    storage_control = client.storage_control
    export_control = client.export_control
    power_control = client.power_control
    assert storage_control is not None
    assert export_control is not None
    assert power_control is not None
    assert {
        "common": _dump(client.common),
        "inverter": _dump(client.inverter),
        "storage_control": _dump(storage_control),
        "export_control": _dump(export_control),
        "power_control": _dump(power_control),
    } == snapshot


async def test_meter_readings(mock_modbus_unit: MockModbusUnit) -> None:
    """A synthetic meter decodes power and energy, honouring word order."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_meter(mock_modbus_unit, index=1)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    meter = client.meters[0]
    assert meter.did is SunSpecDID.THREE_PHASE_WYE_METER
    assert meter.ac_power == -800
    assert meter.ac_current == pytest.approx(8.5)  # 850 * 10**-2
    assert meter.energy_exported == 123000
    assert meter.energy_imported == 456000


async def test_battery_readings(mock_modbus_unit: MockModbusUnit) -> None:
    """A synthetic battery decodes its word-swapped floats and status."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_battery(mock_modbus_unit, offset=0)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    battery = client.batteries[0]
    assert battery.rated_energy == pytest.approx(10000.0)
    assert battery.state_of_energy == pytest.approx(87.5)
    assert battery.dc_power == pytest.approx(-1200.0)
    assert battery.status is BatteryStatus.DISCHARGE


@pytest.mark.parametrize("value", [-0.5, 100.5, float("inf")])
async def test_battery_percentage_out_of_range_is_none(
    mock_modbus_unit: MockModbusUnit, value: float
) -> None:
    """A battery state of energy or health outside 0-100 decodes to None.

    An initializing battery (and the odd communication glitch) reports
    percentages outside the meaningful range; those are garbage, not readings.
    """
    seed(mock_modbus_unit, FIXTURE)
    _seed_battery(mock_modbus_unit, offset=0)
    words = encode_float32(value, word_order="little")
    for i, word in enumerate(words):
        mock_modbus_unit.holding[57730 + i] = word  # state of health
        mock_modbus_unit.holding[57732 + i] = word  # state of energy

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    battery = client.batteries[0]
    assert battery.state_of_health is None
    assert battery.state_of_energy is None


async def test_battery_percentage_boundaries_kept(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The 0 and 100 percent boundaries are genuine readings, not garbage."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_battery(mock_modbus_unit, offset=0)
    for i, word in enumerate(encode_float32(100.0, word_order="little")):
        mock_modbus_unit.holding[57730 + i] = word  # state of health
    for i, word in enumerate(encode_float32(0.0, word_order="little")):
        mock_modbus_unit.holding[57732 + i] = word  # state of energy

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    battery = client.batteries[0]
    assert battery.state_of_health == pytest.approx(100.0)
    assert battery.state_of_energy == pytest.approx(0.0)


async def test_second_battery_uses_base_offset(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A second battery reads from its OWN offset block, not battery 1's.

    The two batteries carry distinct state-of-energy values, so a wrong offset
    (e.g. reading battery 2 from offset 0) would surface as the wrong value.
    """
    seed(mock_modbus_unit, FIXTURE)
    _seed_battery(mock_modbus_unit, offset=0)  # state-of-energy 87.5
    _seed_battery(mock_modbus_unit, offset=256)
    # Give battery 2 a distinct SoE at its own block (57732 + 256).
    for i, word in enumerate(encode_float32(42.0, word_order="little")):
        mock_modbus_unit.holding[57732 + 256 + i] = word

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()

    assert len(client.batteries) == 2
    assert client.batteries[0].state_of_energy == pytest.approx(87.5)
    assert client.batteries[1].state_of_energy == pytest.approx(42.0)


# -- robustness ----------------------------------------------------------------


async def test_battery_energy_unimplemented_is_none(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """An unimplemented 64-bit battery energy accumulator decodes to None."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_battery(mock_modbus_unit, offset=0)
    words = encode_int(0xFFFFFFFFFFFFFFFF, count=4, word_order="little")
    for i, word in enumerate(words):
        mock_modbus_unit.holding[57718 + i] = word

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.batteries[0].energy_exported is None


async def test_battery_float_nan_is_none(mock_modbus_unit: MockModbusUnit) -> None:
    """An unimplemented proprietary float (NaN) decodes to None, not float('nan')."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_battery(mock_modbus_unit, offset=0)
    nan_words = encode_float32(float("nan"), word_order="little")
    for i, word in enumerate(nan_words):
        mock_modbus_unit.holding[57716 + i] = word  # dc_power -> NaN

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.batteries[0].dc_power is None


async def test_out_of_range_scale_factor_decodes_none(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A malformed scale factor that overflows 10**sf decodes to None, not a crash.

    A SunSpec sunssf is meant to be -10..10; a garbled device reporting a large
    exponent must not leak a raw OverflowError. modbus-connection decodes the
    unscalable point to None and the poll still completes. Found by fuzzing.
    """
    seed(mock_modbus_unit, FIXTURE)
    mock_modbus_unit.holding[40084] = 309  # AC_Power_SF: 10**309 overflows
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.inverter.ac_power is None


async def test_site_limit_sentinel_is_none(mock_modbus_unit: MockModbusUnit) -> None:
    """SolarEdge's +/- FLT_MAX 'no limit' sentinel decodes to None."""
    seed(mock_modbus_unit, FIXTURE)
    words = encode_float32(-3.4e38, word_order="little")
    for i, word in enumerate(words):
        mock_modbus_unit.holding[57346 + i] = word

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.export_control is not None
    assert client.export_control.site_limit is None


async def test_site_limit_real_value(mock_modbus_unit: MockModbusUnit) -> None:
    """A real export limit is returned as-is."""
    seed(mock_modbus_unit, FIXTURE)
    words = encode_float32(5000.0, word_order="little")
    for i, word in enumerate(words):
        mock_modbus_unit.holding[57346 + i] = word

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.export_control is not None
    assert client.export_control.site_limit == pytest.approx(5000.0)


# -- features ------------------------------------------------------------------


async def test_grid_status(mock_modbus_unit: MockModbusUnit) -> None:
    """Grid on/off status decodes from the word-swapped status word."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.inverter.on_grid is True  # fixture reports 0 -> on-grid

    words = encode_int(1, count=2, word_order="little")
    for i, word in enumerate(words):
        mock_modbus_unit.holding[40113 + i] = word
    await client.async_update()
    assert client.inverter.on_grid is False


async def test_meter_apparent_reactive_energy(mock_modbus_unit: MockModbusUnit) -> None:
    """Apparent (VAh) and reactive (varh) meter accumulators decode."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_meter(mock_modbus_unit, index=1)
    holding = mock_modbus_unit.holding
    holding[40243] = encode_int(50000, count=2)  # apparent exported
    holding[40259] = 0  # VAh SF
    holding[40260] = encode_int(700, count=2)  # reactive Q1
    holding[40292] = 0  # varh SF

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    meter = client.meters[0]
    assert meter.apparent_energy_exported == 50000
    assert meter.reactive_energy_q1 == 700


async def test_mmppt_modules(mock_modbus_unit: MockModbusUnit) -> None:
    """A multiple-MPPT block is detected and its modules decode with scaling."""
    seed(mock_modbus_unit, FIXTURE)
    _seed_mmppt(mock_modbus_unit, modules=2)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.mmppt is not None
    await client.async_update()

    modules = client.mmppt.modules
    assert len(modules) == 2
    assert modules[0].module_id == 1
    assert modules[0].dc_current == pytest.approx(8.5)  # 850 * 10**-2
    assert modules[0].dc_voltage == pytest.approx(390.0)  # 3900 * 10**-1
    assert modules[0].dc_power == 3300
    assert modules[0].temperature == 45
    assert modules[1].module_id == 2


async def test_mmppt_absent(mock_modbus_unit: MockModbusUnit) -> None:
    """An inverter without the MMPPT extension exposes no mmppt component."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.mmppt is None


async def test_mmppt_garbage_module_count(mock_modbus_unit: MockModbusUnit) -> None:
    """An MMPPT header with an out-of-range module count is not treated as MMPPT."""
    seed(mock_modbus_unit, FIXTURE)
    mock_modbus_unit.holding[40121] = int(SunSpecDID.MULTIPLE_MPPT)
    mock_modbus_unit.holding[40129] = 1  # only 2 or 3 modules are supported
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.mmppt is None


async def test_mmppt_module_count_is_clamped(mock_modbus_unit: MockModbusUnit) -> None:
    """A device reporting an implausible module count is clamped, not trusted.

    The probe validates the count, but the component re-reads it on every poll;
    a huge count would otherwise drive an unbounded read.
    """
    seed(mock_modbus_unit, FIXTURE)
    _seed_mmppt(mock_modbus_unit, modules=2)
    mock_modbus_unit.holding[40129] = 5000  # hostile/garbled count
    client = SolarEdge(mock_modbus_unit, mmppt=True)
    await client.async_update()
    assert client.mmppt is not None
    assert len(client.mmppt.modules) == 3  # clamped to the SolarEdge maximum


async def test_real_inverter_is_not_ev_charger(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A normal inverter is not flagged as an EV charger."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.is_ev_charger is False


async def test_is_ev_charger_unknown_before_update(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Before the first update the model is unknown, so the answer is None."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.is_ev_charger is None


def test_constructor_rejects_out_of_range_counts(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The constructor guards against meter/battery counts it cannot address."""
    with pytest.raises(SolarEdgeError, match="meters"):
        SolarEdge(mock_modbus_unit, meters=4)
    with pytest.raises(SolarEdgeError, match="batteries"):
        SolarEdge(mock_modbus_unit, batteries=4)


async def test_mmppt_shifts_meters(mock_modbus_unit: MockModbusUnit) -> None:
    """With MMPPT present, meters are detected and read at their shifted block.

    Two MPPT modules shift the meter block up by 50 registers, including the
    scale-factor registers. The scaled current (SF -2) only decodes correctly if
    the scale register moved with the block.
    """
    seed(mock_modbus_unit, FIXTURE)
    _seed_mmppt(mock_modbus_unit, modules=2)  # shift = 10 + 2 * 20 = 50
    _seed_meter(mock_modbus_unit, index=1, shift=50)

    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.mmppt is not None
    assert len(client.meters) == 1
    await client.async_update()

    meter = client.meters[0]
    assert meter.did is SunSpecDID.THREE_PHASE_WYE_METER
    assert meter.ac_power == -800
    assert meter.ac_current == pytest.approx(8.5)  # proves scale register shifted
    assert meter.energy_exported == 123000


async def test_export_control_mode_bitfield(mock_modbus_unit: MockModbusUnit) -> None:
    """E_Lim_Ctl_Mode is decoded as a bitfield, not a plain enum value."""
    seed(mock_modbus_unit, FIXTURE)  # fixture reports 0 -> disabled
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.export_control is not None
    assert client.export_control.mode is None

    mock_modbus_unit.holding[57344] = 1  # bit 0 -> export/import meter
    await client.async_update()
    assert (
        client.export_control.mode
        is ExportControlMode.EXPORT_CONTROL_EXPORT_IMPORT_METER
    )

    mock_modbus_unit.holding[57344] = (1 << 2) | (1 << 11)  # bit 2 + unrelated bit
    await client.async_update()
    assert client.export_control.mode is ExportControlMode.PRODUCTION_CONTROL

    # When several mode bits are set, the lowest (highest priority) wins.
    mock_modbus_unit.holding[57344] = (1 << 0) | (1 << 2)  # bit 0 and bit 2
    await client.async_update()
    assert (
        client.export_control.mode
        is ExportControlMode.EXPORT_CONTROL_EXPORT_IMPORT_METER  # bit 0 wins
    )


async def test_export_control_set_mode(mock_modbus_unit: MockModbusUnit) -> None:
    """Setting the mode flips only the mode bits, preserving the rest."""
    seed(mock_modbus_unit, FIXTURE)
    mock_modbus_unit.holding[57344] = 1 << 11  # an unrelated status bit to preserve
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.export_control is not None

    await client.export_control.set_mode(ExportControlMode.PRODUCTION_CONTROL)
    assert mock_modbus_unit.holding[57344] == (1 << 2) | (1 << 11)


async def test_export_control_set_mode_none_disables(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """set_mode(None) clears the mode bits, disabling export limiting."""
    seed(mock_modbus_unit, FIXTURE)
    mock_modbus_unit.holding[57344] = (1 << 1) | (1 << 11)  # a mode bit + status bit
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.export_control is not None

    await client.export_control.set_mode(None)
    assert mock_modbus_unit.holding[57344] == 1 << 11  # only the status bit remains


async def test_export_mode_unimplemented_is_none(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """An unimplemented export-mode register (0xFFFF) decodes mode to None."""
    seed(mock_modbus_unit, FIXTURE)
    mock_modbus_unit.holding[57344] = 0xFFFF  # integer nan sentinel
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.export_control is not None
    assert client.export_control.mode is None
    assert client.export_control.external_production is None


async def test_direct_write_registers_wraps_connection_error(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    """A failed multi-register control write (set_enabled) wraps the backend error."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.advanced_power_control is not None
    mock_modbus_connection.simulate_connection_lost()

    with pytest.raises(SolarEdgeConnectionError):
        await client.advanced_power_control.set_enabled(enabled=True)


async def test_export_bit_switches(mock_modbus_unit: MockModbusUnit) -> None:
    """External-production and negative-site-limit are bit toggles on 57344."""
    seed(mock_modbus_unit, FIXTURE)
    mock_modbus_unit.holding[57344] = 1  # mode bit 0 set, to be preserved
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    export = client.export_control
    assert export is not None
    assert export.external_production is False
    assert export.negative_site_limit is False

    # Two toggles in a row, without an intervening refresh: the second must see
    # the first (the setters keep the decoded cache in step with the register).
    await export.set_external_production(enabled=True)
    assert mock_modbus_unit.holding[57344] == 1 | (1 << 10)

    await export.set_negative_site_limit(enabled=True)
    assert mock_modbus_unit.holding[57344] == 1 | (1 << 10) | (1 << 11)


async def test_external_production_max_write(mock_modbus_unit: MockModbusUnit) -> None:
    """External production max round-trips through the word-swapped float."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.export_control is not None
    await client.export_control.write("external_production_max", 5000.0)
    words = _words(mock_modbus_unit.holding, 57362, 57363)
    assert decode_float32(words, word_order="little") == pytest.approx(5000.0)


async def test_advanced_power_control(mock_modbus_unit: MockModbusUnit) -> None:
    """Advanced power control reads config/enable and writes its controls."""
    seed(mock_modbus_unit, FIXTURE)
    for i, word in enumerate(encode_int(3, count=2, word_order="little")):
        mock_modbus_unit.holding[61700 + i] = word  # reactive config = QU_QP
    for i, word in enumerate(encode_int(1, count=2, word_order="little")):
        mock_modbus_unit.holding[61704 + i] = word  # AdvPwrCtrlEn = 1

    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.advanced_power_control is not None
    await client.async_update()

    advanced = client.advanced_power_control
    assert advanced.reactive_power_config is ReactivePowerConfig.QU_QP
    assert advanced.enabled is True

    await advanced.write("reactive_power_config", ReactivePowerConfig.COSPHI_P)
    assert mock_modbus_unit.holding[61700] == int(ReactivePowerConfig.COSPHI_P)

    await advanced.set_enabled(enabled=False)
    assert mock_modbus_unit.holding[61762] == 0

    await advanced.commit()
    assert mock_modbus_unit.holding[61696] == 1

    await advanced.restore_defaults()
    assert mock_modbus_unit.holding[61697] == 1

    await advanced.set_current_limit(16.0)
    words = _words(mock_modbus_unit.holding, 61838, 61839)
    assert decode_float32(words, word_order="little") == pytest.approx(16.0)

    await advanced.set_reactive_power(2500.0)
    words = _words(mock_modbus_unit.holding, 61760, 61761)
    assert decode_float32(words, word_order="little") == pytest.approx(2500.0)


async def test_reactive_power_config_sentinel_is_none(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The int32 'unimplemented' sentinel on the word-swapped register -> None.

    The device word-swaps this register, so the canonical 0x80000000 sentinel
    arrives as wire words [0x0000, 0x8000]. Decoding with little word order must
    recover the sentinel and yield None, not a bogus enum value.
    """
    seed(mock_modbus_unit, FIXTURE)
    # 0x80000000 word-swapped onto the wire.
    for i, word in enumerate(encode_int(0x8000_0000, count=2, word_order="little")):
        mock_modbus_unit.holding[61700 + i] = word

    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    assert client.advanced_power_control is not None
    assert client.advanced_power_control.reactive_power_config is None


async def test_write_refreshes_decoded_cache(mock_modbus_unit: MockModbusUnit) -> None:
    """A write updates the decoded attribute without an intervening update."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    await client.async_update()
    storage = client.storage_control
    assert storage is not None

    await storage.write("backup_reserve", 25.0)
    assert storage.backup_reserve == pytest.approx(25.0)


# -- writes --------------------------------------------------------------------


async def test_write_power_limit(mock_modbus_unit: MockModbusUnit) -> None:
    """Writing the active power limit lands on the right register."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    await client.power_control.write("active_power_limit", 80)
    assert mock_modbus_unit.holding[61441] == 80


async def test_write_storage_control_mode(mock_modbus_unit: MockModbusUnit) -> None:
    """Writing an enum-typed control field encodes its integer value."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.storage_control is not None
    await client.storage_control.write(
        "control_mode", StorageControlMode.MAXIMIZE_SELF_CONSUMPTION
    )
    assert mock_modbus_unit.holding[57348] == int(
        StorageControlMode.MAXIMIZE_SELF_CONSUMPTION
    )


async def test_typed_control_setters(mock_modbus_unit: MockModbusUnit) -> None:
    """Every writable control field has a typed setter that lands on its register."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    storage = client.storage_control
    power = client.power_control
    export = client.export_control
    assert storage is not None
    assert power is not None
    assert export is not None
    holding = mock_modbus_unit.holding

    def written_float(low: int, high: int) -> float:
        return decode_float32(_words(holding, low, high), word_order="little")

    # Storage control: enums, integer timeout, and word-swapped floats.
    await storage.set_control_mode(StorageControlMode.BACKUP_ONLY)
    assert holding[57348] == int(StorageControlMode.BACKUP_ONLY)
    await storage.set_ac_charge_policy(StorageChargePolicy.ALWAYS)
    assert holding[57349] == int(StorageChargePolicy.ALWAYS)
    await storage.set_default_mode(StorageMode.SOLAR_ONLY)
    assert holding[57354] == int(StorageMode.SOLAR_ONLY)
    await storage.set_command_mode(StorageMode.CHARGE_FROM_SOLAR)
    assert holding[57357] == int(StorageMode.CHARGE_FROM_SOLAR)
    await storage.set_command_timeout(1800)
    assert holding[57355] == 1800
    await storage.set_ac_charge_limit(1500.0)
    assert written_float(57350, 57351) == pytest.approx(1500.0)
    await storage.set_backup_reserve(25.0)
    assert written_float(57352, 57353) == pytest.approx(25.0)
    await storage.set_charge_limit(2000.0)
    assert written_float(57358, 57359) == pytest.approx(2000.0)
    await storage.set_discharge_limit(2500.0)
    assert written_float(57360, 57361) == pytest.approx(2500.0)

    # Power control.
    await power.set_active_power_limit(75)
    assert holding[61441] == 75
    await power.set_cos_phi(0.95)
    assert written_float(61442, 61443) == pytest.approx(0.95)

    # Export control.
    await export.set_site_limit(5000.0)
    assert written_float(57346, 57347) == pytest.approx(5000.0)
    await export.set_external_production_max(3000.0)
    assert written_float(57362, 57363) == pytest.approx(3000.0)


async def test_write_readonly_field_raises(mock_modbus_unit: MockModbusUnit) -> None:
    """Writing a read-only field is rejected."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    with pytest.raises(AttributeError):
        await client.inverter.write("ac_power", 1)


async def test_write_out_of_range_int_raises_solaredge_error(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A value too large for the field's register raises SolarEdgeError, not raw.

    Found by fuzzing: the encoder otherwise leaks an OverflowError.
    """
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    with pytest.raises(SolarEdgeError, match="out of range"):
        await client.power_control.write("active_power_limit", 99999)


async def test_write_out_of_range_float_raises_solaredge_error(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A float too large for a float32 register raises SolarEdgeError, not raw."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.storage_control is not None
    assert client.advanced_power_control is not None
    with pytest.raises(SolarEdgeError, match="out of range"):
        await client.storage_control.write("backup_reserve", 1e40)
    with pytest.raises(SolarEdgeError, match="encoded"):
        await client.advanced_power_control.set_current_limit(1e40)


async def test_write_rejects_semantically_invalid_values(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Control fields reject out-of-range setpoints instead of silently wrapping."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    assert client.storage_control is not None
    cases = [
        (client.power_control, "active_power_limit", -1),  # would wrap to 65535
        (client.power_control, "active_power_limit", 101),  # above 100%
        (client.power_control, "cos_phi", 2.0),  # outside -1..1
        (client.storage_control, "backup_reserve", -5),
        (client.storage_control, "charge_limit", -10),
    ]
    for component, field, value in cases:
        with pytest.raises(SolarEdgeError, match="out of range"):
            await component.write(field, value)
    # A rejected write never reaches the register.
    assert mock_modbus_unit.holding.get(61441) != 0xFFFF


async def test_write_rejects_non_numeric_values(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A non-numeric value on a validated field is rejected with a clear error."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    assert client.storage_control is not None
    with pytest.raises(SolarEdgeError, match="must be a number"):
        await client.power_control.write("active_power_limit", "80")
    with pytest.raises(SolarEdgeError, match="must be a number"):
        await client.storage_control.write("charge_limit", None)


async def test_write_encode_overflow_backstop(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A value that passes validation but can't be encoded still wraps cleanly."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.storage_control is not None
    # command_timeout only requires >= 0, but this overflows its uint32 register.
    with pytest.raises(SolarEdgeError, match="out of range"):
        await client.storage_control.write("command_timeout", 10**40)


async def test_write_none_wraps_as_solaredge_error(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Writing None surfaces as SolarEdgeError, not a raw TypeError."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.storage_control is not None
    with pytest.raises(SolarEdgeError):
        await client.storage_control.write("control_mode", None)


async def test_write_invalid_enum_code_is_rejected(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """An unknown enum code is rejected instead of being written to the register."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.storage_control is not None
    before = mock_modbus_unit.holding.get(57348)
    with pytest.raises(SolarEdgeError, match="not a valid StorageControlMode"):
        await client.storage_control.write("control_mode", 99)
    # A bool must not sneak through as an enum code either.
    with pytest.raises(SolarEdgeError, match="must be a StorageControlMode"):
        await client.storage_control.write("control_mode", True)  # noqa: FBT003
    # The bad values never reached the register.
    assert mock_modbus_unit.holding.get(57348) == before


async def test_direct_control_writes_wrap_bad_values(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The direct advanced-control setters wrap bad values, not leak raw errors."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.advanced_power_control is not None
    with pytest.raises(SolarEdgeError):
        await client.advanced_power_control.set_current_limit(None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(SolarEdgeError):
        await client.advanced_power_control.set_reactive_power(None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_write_cache_reflects_device_readback(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """The optimistic cache stores the decoded readback, not the raw input."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    assert client.storage_control is not None

    # float32 cannot hold 0.9 exactly; the cache must match a real read.
    await client.power_control.write("cos_phi", 0.9)
    words = _words(mock_modbus_unit.holding, 61442, 61443)
    assert client.power_control.cos_phi == decode_float32(words, word_order="little")
    assert client.power_control.cos_phi != 0.9

    # A raw int written to an enum field caches the enum member, not the int.
    await client.storage_control.write("control_mode", 1)
    assert (
        client.storage_control.control_mode
        is StorageControlMode.MAXIMIZE_SELF_CONSUMPTION
    )


# -- error handling ------------------------------------------------------------


async def test_update_blank_inverter_block_raises(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A device answering with a bogus inverter identity is surfaced, not hidden.

    A zero or unknown model id decodes to None; the guard treats a missing
    inverter identity as a read failure rather than returning a silently-blank
    inverter. (A genuine partial block read raises upstream and is wrapped as a
    connection error separately.)
    """
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    mock_modbus_unit.holding[40069] = 0  # inverter DID -> not a member -> None

    with pytest.raises(SolarEdgeConnectionError, match="no valid inverter data"):
        await client.async_update()


async def test_update_partial_block_read_raises(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A block refused mid-poll surfaces as a connection error, not silent None.

    modbus-connection raises on a partial block read rather than blanking the
    values that did not come back (a ``BlockReadError``, itself a ``ModbusError``).
    Here the always-present inverter block is refused after a clean probe, and
    ``async_update`` wraps that as a connection error instead of handing back a
    half-empty device.
    """
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)

    # Refuse any pooled read that spans the inverter model block.
    mock_modbus_unit.fail_read(40069, ModbusExceptionError(4))

    with pytest.raises(SolarEdgeConnectionError):
        await client.async_update()


async def test_update_wraps_connection_error(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    """A dropped link during update surfaces as SolarEdgeConnectionError."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    mock_modbus_connection.simulate_connection_lost()
    with pytest.raises(SolarEdgeConnectionError):
        await client.async_update()


async def test_probe_wraps_connection_error(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    """A dropped link during probe surfaces as SolarEdgeConnectionError."""
    mock_modbus_connection.simulate_connection_lost()
    with pytest.raises(SolarEdgeConnectionError):
        await SolarEdge.async_probe(mock_modbus_unit)


async def test_write_wraps_connection_error(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    """A failed field write surfaces as SolarEdgeConnectionError, like reads."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    mock_modbus_connection.simulate_connection_lost()

    with pytest.raises(SolarEdgeConnectionError):
        await client.power_control.write("active_power_limit", 50)


async def test_advanced_control_write_wraps_connection_error(
    mock_modbus_connection: MockModbusConnection, mock_modbus_unit: MockModbusUnit
) -> None:
    """A failed direct control write (commit) also wraps the backend error."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.advanced_power_control is not None
    mock_modbus_connection.simulate_connection_lost()

    with pytest.raises(SolarEdgeConnectionError):
        await client.advanced_power_control.commit()


async def test_probe_timeout_wraps_connection_error(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A Modbus timeout during probe wraps like any other backend error.

    ``ModbusTimeoutError`` also subclasses ``TimeoutError``/``OSError``, so this
    pins that the ``except ModbusError`` still catches it.
    """
    seed(mock_modbus_unit, FIXTURE)
    unit = _PickyUnit(
        mock_modbus_unit, fail_at={40000}, error=ModbusTimeoutError("timed out")
    )
    with pytest.raises(SolarEdgeConnectionError):
        await SolarEdge.async_probe(unit)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_write_failure_leaves_register_unchanged(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A timed-out write raises and does not mutate the target register."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)
    assert client.power_control is not None
    before = mock_modbus_unit.holding[61441]
    mock_modbus_unit.fail_write(61441, ModbusTimeoutError("timed out"))

    with pytest.raises(SolarEdgeConnectionError):
        await client.power_control.write("active_power_limit", 99)
    assert mock_modbus_unit.holding[61441] == before


async def test_concurrent_updates_are_consistent(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Two updates in flight at once both succeed with correct values."""
    seed(mock_modbus_unit, FIXTURE)
    client = await SolarEdge.async_probe(mock_modbus_unit)

    await asyncio.gather(client.async_update(), client.async_update())

    assert client.inverter.status is InverterStatus.PRODUCING
    assert client.inverter.ac_frequency == pytest.approx(50.0, abs=1.0)


async def test_update_recovers_after_transient_error(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """A single timed-out read fails one update; the next retry recovers."""
    seed(mock_modbus_unit, FIXTURE)
    unit = _TransientUnit(mock_modbus_unit)
    client = await SolarEdge.async_probe(unit)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    unit.fail_next_read()
    with pytest.raises(SolarEdgeConnectionError):
        await client.async_update()

    # The client is not wedged: a retry reads and decodes normally.
    await client.async_update()
    assert client.inverter.status is InverterStatus.PRODUCING
