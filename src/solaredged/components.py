"""Device components for SolarEdge, modelled on ``modbus-connection``.

Each :class:`~modbus_connection.model.Component` maps one sub-system's registers
to typed attributes. The inverter and meter blocks are standard SunSpec, so they
use the big-endian ``sunspec`` factories with automatic scale-factor handling and
"unimplemented" sentinels. The SolarEdge proprietary blocks (battery, storage,
export, power and advanced power control) are word-swapped, so their multi-register
fields use the generic factories with ``word_order="little"`` (single-register
fields need no word order).

Values decode to ``None`` when the device reports a point as unimplemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modbus_connection import ModbusError
from modbus_connection.encode import encode_float32, encode_int
from modbus_connection.model import (
    Component,
    enum,
    integer,
    repeating_group,
)
from modbus_connection.model import int32 as int32_le
from modbus_connection.model import uint32 as uint32_le
from modbus_connection.model.fields import (
    FloatField,
    NumberField,
    StringField,
)
from modbus_connection.model.sunspec import (
    bitfield32,
    enum16,
    int16,
    string,
    uint16,
    uint32,
)

from .const import (
    EXPORT_EXTERNAL_PRODUCTION_BIT,
    EXPORT_NEGATIVE_SITE_LIMIT_BIT,
    METER_STRIDE,
    BatteryStatus,
    ExportControlLimit,
    ExportControlMode,
    InverterStatus,
    MeterEvent,
    ReactivePowerConfig,
    StorageChargePolicy,
    StorageControlMode,
    StorageMode,
    SunSpecDID,
)
from .exceptions import SolarEdgeConnectionError, SolarEdgeError

if TYPE_CHECKING:
    from collections.abc import Callable
    from enum import IntEnum


# -- write validators ----------------------------------------------------------
# A control field's ``writable`` may be a validator that vets the requested value
# before it is encoded. These reject out-of-range values with a clear error, so a
# bad setpoint fails loudly instead of silently wrapping (e.g. -1 -> 65535).


def _bounded(low: float, high: float) -> Callable[[Any], Any]:
    """Build a validator that rejects values outside the inclusive range."""

    def _validate(value: Any) -> Any:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            msg = f"value must be a number, got {value!r}"
            raise SolarEdgeError(msg)

        if not low <= value <= high:
            msg = f"value {value} is out of range (expected {low} to {high})"
            raise SolarEdgeError(msg)

        return value

    return _validate


def _at_least(low: float) -> Callable[[Any], Any]:
    """Build a validator that rejects values below ``low``."""

    def _validate(value: Any) -> Any:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            msg = f"value must be a number, got {value!r}"
            raise SolarEdgeError(msg)

        if value < low:
            msg = f"value {value} is out of range (expected at least {low})"
            raise SolarEdgeError(msg)

        return value

    return _validate


def _enum_member(enum_type: type[IntEnum]) -> Callable[[Any], Any]:
    """Build a validator accepting a member of ``enum_type`` (or its raw code).

    Rejects unknown codes so an invalid command is not silently written to a
    live control register (where it would encode the raw int and decode back to
    None). Returns the resolved member, which is what gets written.
    """

    def _validate(value: Any) -> Any:
        if isinstance(value, bool):
            msg = f"value must be a {enum_type.__name__}, got {value!r}"
            raise SolarEdgeError(msg)

        try:
            return enum_type(value)
        except (ValueError, TypeError) as err:
            valid = ", ".join(member.name for member in enum_type)
            msg = (
                f"value {value!r} is not a valid {enum_type.__name__} "
                f"(expected one of: {valid})"
            )
            raise SolarEdgeError(msg) from err

    return _validate


# -- meter field helpers -------------------------------------------------------
# Every meter field carries the per-meter stride so meters 2 and 3 read from
# their own blocks; scaled fields shift their scale register by the same stride.


def _meter_int16(address: int, scale_register: int) -> NumberField[float]:
    """Build a scaled signed 16-bit meter point."""
    return int16(
        address,
        scale_register=scale_register,
        scale_register_stride=METER_STRIDE,
        stride=METER_STRIDE,
    )


def _meter_energy(
    address: int, scale_register: int, unit: str = "Wh"
) -> NumberField[float]:
    """Build a scaled 32-bit meter accumulator (real Wh, apparent VAh or varh).

    SunSpec types these as ``acc32`` (sentinel 0 = "not accumulated"), but we
    model them as ``uint32`` on purpose: a genuine 0 is a valid reading on a
    freshly commissioned meter, so it should decode to 0, not None. Do not
    "fix" this back to ``acc32`` or real zeros start disappearing.
    """
    return uint32(
        address,
        scale_register=scale_register,
        scale_register_stride=METER_STRIDE,
        stride=METER_STRIDE,
        unit=unit,
    )


def _meter_string(address: int, length: int) -> StringField:
    """Build a meter identity string."""
    return string(address, length, stride=METER_STRIDE)


# -- proprietary block helpers ------------------------------------------------
# The battery and control blocks are word-swapped and carry their own sentinels.

# SolarEdge reports an unset float limit as +/- FLT_MAX rather than NaN.
_FLOAT_NO_LIMIT = 1e37
# uint64 "not implemented" sentinel (all bits set).
_UINT64_NAN = 0xFFFF_FFFF_FFFF_FFFF
# float32 "not implemented" sentinel (IEEE-754 NaN).
_FLOAT32_NAN = 0x7FC0_0000


def _battery_energy(address: int) -> NumberField[int]:
    """Build a little-endian 64-bit battery energy accumulator (Wh), sentinel-aware."""
    return NumberField(
        address,
        count=4,
        signed=False,
        nan=_UINT64_NAN,
        word_order="little",
        unit="Wh",
    )


def _le_float32(
    address: int,
    *,
    writable: bool | Callable[[Any], Any] = False,
    unit: str | None = None,
) -> FloatField:
    """Build a word-swapped float32 that decodes SolarEdge's NaN sentinel to None."""
    return FloatField(
        address,
        count=2,
        nan=_FLOAT32_NAN,
        word_order="little",
        writable=writable,
        unit=unit,
    )


class SolarEdgeComponent(Component):
    """A ``Component`` whose writes raise the library's own connection error.

    ``modbus-connection`` raises backend errors on a failed write; wrapping them
    here keeps the public surface consistent with reads (which are wrapped in
    ``async_update`` / ``async_probe``).
    """

    async def write(self, field: str, value: Any) -> None:
        """Write a field by name (a low-level, generic escape hatch).

        Prefer the typed ``set_*`` methods on each control component where they
        exist; they are discoverable and check the value type at the call site.

        Raises :class:`SolarEdgeConnectionError` on a backend/transport failure
        and :class:`SolarEdgeError` when the value is rejected by the field's
        validator or does not fit its register(s).

        On success the decoded cache is updated optimistically, so reading the
        attribute back reflects the write without waiting for the next
        ``async_update``. Without this, a write followed by a read returns the
        stale pre-write value, and sequential writes that read-modify-write the
        same register clobber each other.
        """
        try:
            await super().write(field, value)
        except ModbusError as err:
            raise SolarEdgeConnectionError(str(err)) from err
        except (ArithmeticError, TypeError, ValueError) as err:
            # A value that does not fit the field's register(s) (too large, wrong
            # type, or otherwise unencodable) must surface as our own error, not a
            # raw OverflowError/TypeError from the encoder.
            msg = f"Value {value!r} is out of range for field {field!r}"
            raise SolarEdgeError(msg) from err

        # The write landed; refresh the cache. Store the value the device would
        # report back, not the raw input, so a read-after-write matches a later
        # poll (float rounding, an enum member, a NaN normalising to None). Only
        # register fields have a cache; this library has no writable bit fields.
        if field in self._register_fields:  # pragma: no branch
            codec = self._register_fields[field]
            self._values[field] = codec.decode(codec.encode(value))

    async def _write_register(self, address: int, value: int) -> None:
        """Write a single register directly, with the same error translation."""
        try:
            await self._unit.write_register(address, value)
        except ModbusError as err:
            raise SolarEdgeConnectionError(str(err)) from err

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """Write consecutive registers directly, with the same error translation."""
        try:
            await self._unit.write_registers(address, values)
        except ModbusError as err:
            raise SolarEdgeConnectionError(str(err)) from err

    @staticmethod
    def _encode_float32_le(value: float) -> list[int]:
        """Encode a word-swapped float32, rejecting unencodable values cleanly."""
        try:
            return encode_float32(value, word_order="little")
        except (ArithmeticError, TypeError, ValueError) as err:
            msg = f"Value {value!r} cannot be encoded as a float32 register"
            raise SolarEdgeError(msg) from err


class Common(SolarEdgeComponent):
    """Inverter identity (SunSpec common block, base 40000)."""

    manufacturer = string(40004, 16)
    model = string(40020, 16)
    option = string(40036, 8)
    version = string(40044, 8)
    serial_number = string(40052, 16)
    device_address = uint16(40068)


class Inverter(SolarEdgeComponent):
    """Inverter measurements and state (SunSpec model 101/102/103, base 40069)."""

    did = enum16(40069, SunSpecDID)

    ac_current = uint16(40071, scale_register=40075, unit="A")
    ac_current_a = uint16(40072, scale_register=40075, unit="A")
    ac_current_b = uint16(40073, scale_register=40075, unit="A")
    ac_current_c = uint16(40074, scale_register=40075, unit="A")

    ac_voltage_ab = uint16(40076, scale_register=40082, unit="V")
    ac_voltage_bc = uint16(40077, scale_register=40082, unit="V")
    ac_voltage_ca = uint16(40078, scale_register=40082, unit="V")
    ac_voltage_an = uint16(40079, scale_register=40082, unit="V")
    ac_voltage_bn = uint16(40080, scale_register=40082, unit="V")
    ac_voltage_cn = uint16(40081, scale_register=40082, unit="V")

    ac_power = int16(40083, scale_register=40084, unit="W")
    ac_frequency = uint16(40085, scale_register=40086, unit="Hz")
    ac_va = int16(40087, scale_register=40088, unit="VA")
    ac_var = int16(40089, scale_register=40090, unit="var")
    ac_power_factor = int16(40091, scale_register=40092, unit="%")
    ac_energy = uint32(40093, scale_register=40095, unit="Wh")

    dc_current = uint16(40096, scale_register=40097, unit="A")
    dc_voltage = uint16(40098, scale_register=40099, unit="V")
    dc_power = int16(40100, scale_register=40101, unit="W")

    temperature_cabinet = int16(40102, scale_register=40106, unit="°C")
    temperature_heatsink = int16(40103, scale_register=40106, unit="°C")
    temperature_transformer = int16(40104, scale_register=40106, unit="°C")
    temperature_other = int16(40105, scale_register=40106, unit="°C")

    status = enum16(40107, InverterStatus)
    vendor_status = uint16(40108)

    # Grid on/off status (word-swapped uint32) and extended vendor status. Both
    # sit inside the always-present model block, so they need no separate probe.
    _grid_status = NumberField(
        40113, count=2, signed=False, nan=0xFFFF_FFFF, word_order="little"
    )
    vendor_status_extended = uint32(40119)

    @property
    def on_grid(self) -> bool | None:
        """Whether the inverter is grid-connected, or None when not reported.

        SolarEdge reports on-grid as a zero status word and off-grid as non-zero.
        """
        raw = self._grid_status
        return None if raw is None else raw == 0


class Meter(SolarEdgeComponent):
    """A SolarEdge meter (SunSpec model 201-204).

    One class serves all three meters: instantiate with ``index`` 1, 2 or 3 and
    every address shifts by ``METER_STRIDE`` per meter. Meter 1's addresses are
    declared here. When a multiple-MPPT extension shifts the whole meter block
    up, pass that shift as ``base_offset``; it moves every address, scale
    registers included.
    """

    # Identity (common block, meter 1 base 40121).
    manufacturer = _meter_string(40123, 16)
    model = _meter_string(40139, 16)
    option = _meter_string(40155, 8)
    version = _meter_string(40163, 8)
    serial_number = _meter_string(40171, 16)

    # Measurements (model block, meter 1 base 40188).
    did = enum16(40188, SunSpecDID, stride=METER_STRIDE)

    ac_current = _meter_int16(40190, 40194)
    ac_current_a = _meter_int16(40191, 40194)
    ac_current_b = _meter_int16(40192, 40194)
    ac_current_c = _meter_int16(40193, 40194)

    ac_voltage_ln = _meter_int16(40195, 40203)
    ac_voltage_an = _meter_int16(40196, 40203)
    ac_voltage_bn = _meter_int16(40197, 40203)
    ac_voltage_cn = _meter_int16(40198, 40203)
    ac_voltage_ll = _meter_int16(40199, 40203)
    ac_voltage_ab = _meter_int16(40200, 40203)
    ac_voltage_bc = _meter_int16(40201, 40203)
    ac_voltage_ca = _meter_int16(40202, 40203)

    ac_frequency = _meter_int16(40204, 40205)

    ac_power = _meter_int16(40206, 40210)
    ac_power_a = _meter_int16(40207, 40210)
    ac_power_b = _meter_int16(40208, 40210)
    ac_power_c = _meter_int16(40209, 40210)

    ac_va = _meter_int16(40211, 40215)
    ac_va_a = _meter_int16(40212, 40215)
    ac_va_b = _meter_int16(40213, 40215)
    ac_va_c = _meter_int16(40214, 40215)

    ac_var = _meter_int16(40216, 40220)
    ac_var_a = _meter_int16(40217, 40220)
    ac_var_b = _meter_int16(40218, 40220)
    ac_var_c = _meter_int16(40219, 40220)

    ac_power_factor = _meter_int16(40221, 40225)
    ac_power_factor_a = _meter_int16(40222, 40225)
    ac_power_factor_b = _meter_int16(40223, 40225)
    ac_power_factor_c = _meter_int16(40224, 40225)

    # Real energy (Wh).
    energy_exported = _meter_energy(40226, 40242)
    energy_exported_a = _meter_energy(40228, 40242)
    energy_exported_b = _meter_energy(40230, 40242)
    energy_exported_c = _meter_energy(40232, 40242)
    energy_imported = _meter_energy(40234, 40242)
    energy_imported_a = _meter_energy(40236, 40242)
    energy_imported_b = _meter_energy(40238, 40242)
    energy_imported_c = _meter_energy(40240, 40242)

    # Apparent energy (VAh), totals for exported and imported.
    apparent_energy_exported = _meter_energy(40243, 40259, "VAh")
    apparent_energy_imported = _meter_energy(40251, 40259, "VAh")

    # Reactive energy (varh) per quadrant: Q1/Q2 import, Q3/Q4 export.
    reactive_energy_q1 = _meter_energy(40260, 40292, "varh")
    reactive_energy_q2 = _meter_energy(40268, 40292, "varh")
    reactive_energy_q3 = _meter_energy(40276, 40292, "varh")
    reactive_energy_q4 = _meter_energy(40284, 40292, "varh")

    events = bitfield32(40293, MeterEvent, stride=METER_STRIDE)


class Battery(SolarEdgeComponent):
    """A SolarEdge battery (proprietary block, word-swapped).

    One class serves all batteries: instantiate with the unit's ``base_offset``
    (see ``BATTERY_BASE_OFFSETS``). Battery 1's addresses are declared here.
    """

    # Identity (common block, base 57600).
    manufacturer = string(57600, 16)
    model = string(57616, 16)
    version = string(57632, 16)
    serial_number = string(57648, 16)
    rated_energy = _le_float32(57666, unit="Wh")

    # Telemetry (dynamic block, base 57668).
    max_charge_power = _le_float32(57668, unit="W")
    max_discharge_power = _le_float32(57670, unit="W")
    max_charge_peak_power = _le_float32(57672, unit="W")
    max_discharge_peak_power = _le_float32(57674, unit="W")

    temperature_average = _le_float32(57708, unit="°C")
    temperature_max = _le_float32(57710, unit="°C")

    dc_voltage = _le_float32(57712, unit="V")
    dc_current = _le_float32(57714, unit="A")
    dc_power = _le_float32(57716, unit="W")

    energy_exported = _battery_energy(57718)
    energy_imported = _battery_energy(57722)

    energy_max = _le_float32(57726, unit="Wh")
    energy_available = _le_float32(57728, unit="Wh")

    state_of_health = _le_float32(57730, unit="%")
    state_of_energy = _le_float32(57732, unit="%")

    status = enum(57734, BatteryStatus, count=2, word_order="little", nan=0xFFFFFFFF)


class StorageControl(SolarEdgeComponent):
    """Storage charge/discharge control (proprietary block, base 57348).

    All fields are writable via :meth:`Component.write`; this block is optional
    and only present on inverters with a managed battery.
    """

    control_mode = enum(
        57348, StorageControlMode, nan=0xFFFF, writable=_enum_member(StorageControlMode)
    )
    ac_charge_policy = enum(
        57349,
        StorageChargePolicy,
        nan=0xFFFF,
        writable=_enum_member(StorageChargePolicy),
    )
    ac_charge_limit = _le_float32(57350, writable=_at_least(0))
    backup_reserve = _le_float32(57352, writable=_bounded(0, 100), unit="%")

    # Command settings, applied when control_mode is REMOTE_CONTROL.
    default_mode = enum(
        57354, StorageMode, nan=0xFFFF, writable=_enum_member(StorageMode)
    )
    command_timeout = uint32_le(
        57355, word_order="little", writable=_at_least(0), unit="s"
    )
    command_mode = enum(
        57357, StorageMode, nan=0xFFFF, writable=_enum_member(StorageMode)
    )
    charge_limit = _le_float32(57358, writable=_at_least(0), unit="W")
    discharge_limit = _le_float32(57360, writable=_at_least(0), unit="W")

    async def set_control_mode(self, mode: StorageControlMode) -> None:
        """Set the top-level storage control mode."""
        await self.write("control_mode", mode)

    async def set_ac_charge_policy(self, policy: StorageChargePolicy) -> None:
        """Set the AC charge policy."""
        await self.write("ac_charge_policy", policy)

    async def set_ac_charge_limit(self, limit: float) -> None:
        """Set the AC charge limit (interpretation depends on the charge policy)."""
        await self.write("ac_charge_limit", limit)

    async def set_backup_reserve(self, percent: float) -> None:
        """Set the backup reserve as a percentage of capacity (0-100)."""
        await self.write("backup_reserve", percent)

    async def set_default_mode(self, mode: StorageMode) -> None:
        """Set the default mode, resumed when remote control times out."""
        await self.write("default_mode", mode)

    async def set_command_timeout(self, seconds: int) -> None:
        """Set the remote-command timeout, after which the default mode resumes."""
        await self.write("command_timeout", seconds)

    async def set_command_mode(self, mode: StorageMode) -> None:
        """Set the remote-control command mode (active in REMOTE_CONTROL)."""
        await self.write("command_mode", mode)

    async def set_charge_limit(self, watts: float) -> None:
        """Set the remote charge power limit in watts."""
        await self.write("charge_limit", watts)

    async def set_discharge_limit(self, watts: float) -> None:
        """Set the remote discharge power limit in watts."""
        await self.write("discharge_limit", watts)


class ExportControl(SolarEdgeComponent):
    """Site export / limit control (proprietary block, base 57344).

    Optional; present when export limitation is configured.
    """

    # E_Lim_Ctl_Mode is a bitfield, not a plain enum; read via ``mode`` and
    # write via ``set_mode``, which preserve the other bits.
    _mode_raw = integer(57344, signed=False, nan=0xFFFF, writable=True)
    limit_type = enum(
        57345, ExportControlLimit, nan=0xFFFF, writable=_enum_member(ExportControlLimit)
    )
    # Raw register; write here to set the limit. Read via ``site_limit``.
    site_limit_raw = _le_float32(57346, writable=_at_least(0), unit="W")
    external_production_max = _le_float32(57362, writable=_at_least(0), unit="W")

    @property
    def mode(self) -> ExportControlMode | None:
        """Export limit control mode, or None when disabled/unknown.

        ``E_Lim_Ctl_Mode`` is a bitfield: bit 0/1/2 select the mode (in that
        priority), with no bit set meaning export limiting is disabled.
        """
        raw = self._mode_raw
        if raw is None:
            return None
        for member in ExportControlMode:
            if (raw >> int(member)) & 1:
                return member
        return None

    async def set_mode(self, mode: ExportControlMode | None) -> None:
        """Set the export limit control mode, preserving other status bits.

        Clears the three mode bits and sets the selected one (none for
        ``mode=None``, which disables export limiting).
        """
        raw = (self._mode_raw or 0) & ~0b111
        if mode is not None:
            raw |= 1 << int(mode)
        await self._write_mode(raw)

    @property
    def external_production(self) -> bool | None:
        """Whether an external (non-SolarEdge) production source is configured."""
        raw = self._mode_raw
        return (
            None if raw is None else bool((raw >> EXPORT_EXTERNAL_PRODUCTION_BIT) & 1)
        )

    async def set_external_production(self, *, enabled: bool) -> None:
        """Toggle the external-production flag, preserving other bits."""
        await self._set_mode_bit(EXPORT_EXTERNAL_PRODUCTION_BIT, enabled=enabled)

    @property
    def negative_site_limit(self) -> bool | None:
        """Whether the negative site limit (minimum import) flag is set."""
        raw = self._mode_raw
        return (
            None if raw is None else bool((raw >> EXPORT_NEGATIVE_SITE_LIMIT_BIT) & 1)
        )

    async def set_negative_site_limit(self, *, enabled: bool) -> None:
        """Toggle the negative-site-limit flag, preserving other bits."""
        await self._set_mode_bit(EXPORT_NEGATIVE_SITE_LIMIT_BIT, enabled=enabled)

    async def _set_mode_bit(self, bit: int, *, enabled: bool) -> None:
        """Set or clear a single bit of the export-mode register."""
        raw = self._mode_raw or 0
        raw = raw | (1 << bit) if enabled else raw & ~(1 << bit)
        await self._write_mode(raw)

    async def _write_mode(self, raw: int) -> None:
        """Write the export-mode bitfield.

        ``write`` refreshes the decoded cache, so sequential toggles (mode + bit
        flags) see each other's changes without an intervening ``async_update``.
        """
        await self.write("_mode_raw", raw)

    @property
    def site_limit(self) -> float | None:
        """Site export limit in watts, or None when no limit is configured.

        SolarEdge reports an unset limit as +/- FLT_MAX rather than NaN, so that
        sentinel is normalised to None here.
        """
        value = self.site_limit_raw
        if value is None or abs(value) >= _FLOAT_NO_LIMIT:
            return None
        return value

    async def set_site_limit(self, watts: float) -> None:
        """Set the site export limit in watts."""
        await self.write("site_limit_raw", watts)

    async def set_external_production_max(self, watts: float) -> None:
        """Set the maximum external (non-SolarEdge) production in watts."""
        await self.write("external_production_max", watts)


class PowerControl(SolarEdgeComponent):
    """Dynamic power control (proprietary block, base 61440).

    Optional; exposes the active power limit and power factor setpoint. The
    advanced reactive-power curve tables are not modelled here.
    """

    rrcr_state = integer(61440, signed=False)
    active_power_limit = integer(
        61441, signed=False, writable=_bounded(0, 100), unit="%"
    )
    cos_phi = _le_float32(61442, writable=_bounded(-1.0, 1.0))

    async def set_active_power_limit(self, percent: int) -> None:
        """Set the active power limit as a percentage of nominal (0-100)."""
        await self.write("active_power_limit", percent)

    async def set_cos_phi(self, value: float) -> None:
        """Set the power factor (cos phi) setpoint (-1.0 to 1.0)."""
        await self.write("cos_phi", value)


class AdvancedPowerControl(SolarEdgeComponent):
    """Advanced (reactive) power control (proprietary block, base 61696).

    Optional; used for grid-support features. Word-swapped (little). Setpoint
    changes only take effect after :meth:`commit`.
    """

    # nan is the canonical int32 "unimplemented" value. The device word-swaps
    # this register like every other value in the block, so decoding with
    # word_order="little" recovers 0x8000_0000 and the sentinel matches.
    reactive_power_config = enum(
        61700,
        ReactivePowerConfig,
        count=2,
        signed=True,
        word_order="little",
        nan=0x8000_0000,
        writable=_enum_member(ReactivePowerConfig),
    )
    _enabled = int32_le(61704, word_order="little")

    @property
    def enabled(self) -> bool | None:
        """Whether advanced power control is enabled, or None when unknown."""
        raw = self._enabled
        return None if raw is None else raw == 1

    async def set_enabled(self, *, enabled: bool) -> None:
        """Enable or disable advanced power control (written to register 61762)."""
        await self._write_registers(
            61762, encode_int(1 if enabled else 0, count=2, word_order="little")
        )

    async def set_current_limit(self, amperes: float) -> None:
        """Set the inverter's maximum output current (written to 61838)."""
        await self._write_registers(61838, self._encode_float32_le(amperes))

    async def set_reactive_power(self, watts: float) -> None:
        """Set the fixed reactive-power value (written to 61760)."""
        await self._write_registers(61760, self._encode_float32_le(watts))

    async def commit(self) -> None:
        """Commit pending power-control settings; required for changes to apply."""
        await self._write_register(61696, 1)

    async def restore_defaults(self) -> None:
        """Restore power-control settings to their defaults."""
        await self._write_register(61697, 1)


class MpptModule(SolarEdgeComponent):
    """One DC input (string) of a multiple-MPPT inverter.

    Addresses are for module 0 (base 40131); each further module is read at a
    ``base_offset`` of ``i * 20``. The scale factors live in the shared header
    block, so they are not shifted per module.
    """

    module_id = uint16(40131)
    label = string(40132, 8)
    dc_current = uint16(40140, scale_register=40123, unit="A")
    dc_voltage = uint16(40141, scale_register=40124, unit="V")
    dc_power = uint16(40142, scale_register=40125, unit="W")
    dc_energy = uint32(40143, scale_register=40126, unit="Wh")
    temperature = int16(40147, unit="°C")
    status = integer(40148, signed=False)


class Mmppt(SolarEdgeComponent):
    """Multiple-MPPT extension (SunSpec model 160, base 40121).

    Optional; present on inverters that report per-string DC data. The module
    count is read from the header at 40129 and sizes the ``modules`` list.
    """

    # SolarEdge inverters expose at most three DC inputs (strings).
    _MAX_MODULES = 3

    modules = repeating_group(
        integer(40129, signed=False, nan=0xFFFF), MpptModule, stride=20
    )

    async def async_update_repeating_groups(self) -> None:
        """Read the per-module rows, clamping the device-reported module count.

        The count comes from a device register and sizes an unbounded read; a
        garbled or hostile device reporting a huge count would otherwise drive a
        massive read on every poll. SolarEdge tops out at three strings.
        """
        count = self._counts.get("modules")
        if count is not None and count > self._MAX_MODULES:
            self._counts["modules"] = self._MAX_MODULES

        await super().async_update_repeating_groups()
