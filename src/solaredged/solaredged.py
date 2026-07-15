"""Asynchronous Python client for SolarEdge inverters over Modbus."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from modbus_connection import ModbusError, ModbusExceptionError
from modbus_connection.decode import combine_words, decode_float32
from modbus_connection.model import ComponentGroup

from .components import (
    AdvancedPowerControl,
    Battery,
    Common,
    ExportControl,
    Inverter,
    InverterExtended,
    Meter,
    Mmppt,
    PowerControl,
    StorageControl,
)
from .const import (
    ADVANCED_POWER_CONTROL_BASE,
    BATTERY_BASE_OFFSETS,
    BATTERY_COMMON_BASE,
    EV_CHARGER_MODEL_PREFIX,
    EXPORT_CONTROL_BASE,
    GRID_STATUS_BASE,
    INVERTER_COMMON_BASE,
    METER_COUNT,
    METER_MODEL_BASE,
    METER_STRIDE,
    MMPPT_BASE,
    MMPPT_UNITS_OFFSET,
    POWER_CONTROL_BASE,
    STORAGE_CONTROL_BASE,
    SUNSPEC_ID,
    SunSpecDID,
)
from .exceptions import SolarEdgeConnectionError, SolarEdgeError

if TYPE_CHECKING:
    from modbus_connection import ModbusUnit
    from modbus_connection.model import Component

_METER_DIDS = frozenset(
    {
        SunSpecDID.SINGLE_PHASE_METER,
        SunSpecDID.SPLIT_PHASE_METER,
        SunSpecDID.THREE_PHASE_WYE_METER,
        SunSpecDID.THREE_PHASE_DELTA_METER,
    }
)


class SolarEdge:
    """A single SolarEdge inverter and its attached meters and batteries.

    Bind one instance to one Modbus unit id. The caller owns the
    ``ModbusConnection`` and hands over a ``ModbusUnit`` (obtained with
    ``connection.for_unit(id)``); a multi-inverter site creates one
    :class:`SolarEdge` per unit, all sharing the one connection.

    Prefer :meth:`async_probe` to build an instance: it detects the inverter
    model and which meters, batteries and control blocks are present. The
    constructor is available when the layout is already known.
    """

    def __init__(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        unit: ModbusUnit,
        *,
        meters: int = 0,
        meter_shift: int = 0,
        batteries: int = 0,
        mmppt: bool = False,
        grid_status: bool = False,
        storage_control: bool = False,
        export_control: bool = False,
        power_control: bool = False,
        advanced_power_control: bool = False,
    ) -> None:
        """Set up the components for the detected device layout."""
        if not 0 <= meters <= METER_COUNT:
            msg = f"meters must be between 0 and {METER_COUNT}, got {meters}"
            raise SolarEdgeError(msg)

        if not 0 <= batteries <= len(BATTERY_BASE_OFFSETS):
            msg = (
                f"batteries must be between 0 and {len(BATTERY_BASE_OFFSETS)}, "
                f"got {batteries}"
            )
            raise SolarEdgeError(msg)

        self._unit = unit

        # Always present: the inverter identity and measurements. The grid
        # status extension is firmware-dependent; reading it on an inverter
        # that lacks it would fail the whole pooled inverter read, so the
        # extended component is only used where the extension is present.
        self.common = Common(unit)
        self.inverter = InverterExtended(unit) if grid_status else Inverter(unit)
        self.mmppt = Mmppt(unit) if mmppt else None

        # Attached sub-devices, addressed per unit by index / base offset.
        self.meters: list[Meter] = [
            Meter(unit, index=i + 1, base_offset=meter_shift) for i in range(meters)
        ]
        self.batteries: list[Battery] = [
            Battery(unit, base_offset=BATTERY_BASE_OFFSETS[i]) for i in range(batteries)
        ]

        # Optional writable control blocks.
        self.storage_control = StorageControl(unit) if storage_control else None
        self.export_control = ExportControl(unit) if export_control else None
        self.power_control = PowerControl(unit) if power_control else None
        self.advanced_power_control = (
            AdvancedPowerControl(unit) if advanced_power_control else None
        )

        self._group = ComponentGroup(unit, self.components)

    @property
    def components(self) -> list[Component]:
        """Every present component, in read order."""
        parts: list[Component] = [self.common, self.inverter]
        if self.mmppt is not None:
            parts.append(self.mmppt)

        parts.extend(self.meters)
        parts.extend(self.batteries)
        parts.extend(
            control
            for control in (
                self.storage_control,
                self.export_control,
                self.power_control,
                self.advanced_power_control,
            )
            if control is not None
        )

        return parts

    @property
    def is_ev_charger(self) -> bool | None:
        """Whether this unit is actually a SolarEdge EV charger, not an inverter.

        The charger answers on its own unit id and presents as a SunSpec
        inverter, but exposes no usable telemetry over Modbus, only its
        identity. Recognised by the model name, which is only known after the
        first :meth:`async_update`; returns None until then (the answer is
        genuinely unknown, not False). Use it to skip or relabel such a unit
        rather than treat it as a producing inverter.
        """
        model = self.common.model
        if model is None:
            return None
        return model.startswith(EV_CHARGER_MODEL_PREFIX)

    async def async_update(self) -> None:
        """Refresh every component in one pooled set of Modbus reads."""
        try:
            await self._group.async_update()
        except ModbusError as err:
            raise SolarEdgeConnectionError(str(err)) from err

        # The inverter model id is always populated on a real device. A device
        # that answers the poll but reports a bogus identity (a zero or unknown
        # model id decoding to None) would otherwise present as a silently-blank
        # inverter, so surface that as a read failure. A genuine partial block
        # read raises upstream and is already wrapped above.
        if self.inverter.did is None:
            msg = "Device returned no valid inverter data"
            raise SolarEdgeConnectionError(msg)

    @classmethod
    async def async_probe(cls, unit: ModbusUnit) -> SolarEdge:
        """Detect the device layout on ``unit`` and return a ready instance.

        Validates the SunSpec header, counts the meters, and probes for the
        grid status extension and the battery and control blocks. Absent
        optional blocks (which answer with a Modbus exception) are treated as
        not present; a genuine transport failure raises
        :class:`SolarEdgeConnectionError`.
        """
        try:
            header = await unit.read_holding_registers(INVERTER_COMMON_BASE, 4)

            if combine_words(header[0:2]) != SUNSPEC_ID:
                msg = "Device is not a SolarEdge inverter (no SunSpec identifier)"
                raise SolarEdgeError(msg)

            # A multiple-MPPT extension shifts the meter blocks up by its on-wire
            # size (10 + modules * 20 registers), so detect it before the meters.
            mmppt_units = await cls._mmppt_units(unit)
            meter_shift = 10 + mmppt_units * 20 if mmppt_units else 0

            meters = await cls._count_meters(unit, meter_shift)
            batteries = await cls._count_batteries(unit)

            return cls(
                unit,
                meters=meters,
                meter_shift=meter_shift,
                batteries=batteries,
                mmppt=mmppt_units > 0,
                grid_status=await cls._block_present(unit, GRID_STATUS_BASE),
                storage_control=await cls._block_present(unit, STORAGE_CONTROL_BASE),
                export_control=await cls._block_present(unit, EXPORT_CONTROL_BASE),
                power_control=await cls._block_present(unit, POWER_CONTROL_BASE),
                advanced_power_control=await cls._block_present(
                    unit, ADVANCED_POWER_CONTROL_BASE
                ),
            )
        except ModbusError as err:
            raise SolarEdgeConnectionError(str(err)) from err

    @staticmethod
    async def _mmppt_units(unit: ModbusUnit) -> int:
        """Return the multiple-MPPT module count (2 or 3), or 0 when absent."""
        try:
            header = await unit.read_holding_registers(
                MMPPT_BASE, MMPPT_UNITS_OFFSET + 1
            )
        except ModbusExceptionError:
            return 0

        modules = header[MMPPT_UNITS_OFFSET]
        if header[0] == SunSpecDID.MULTIPLE_MPPT and modules in (2, 3):
            return modules

        return 0

    @staticmethod
    async def _count_meters(unit: ModbusUnit, meter_shift: int = 0) -> int:
        """Count meters by reading each meter's model identifier register."""
        count = 0
        base = METER_MODEL_BASE + meter_shift

        for index in range(METER_COUNT):
            address = base + METER_STRIDE * index
            try:
                did = (await unit.read_holding_registers(address, 1))[0]
            except ModbusExceptionError:
                break

            if did not in _METER_DIDS:
                break

            count += 1

        return count

    @staticmethod
    async def _count_batteries(unit: ModbusUnit) -> int:
        """Count batteries by reading each battery's rated-energy register."""
        count = 0

        # B_RatedEnergy sits at offset 66 in each battery's common block.
        rated_energy_base = BATTERY_COMMON_BASE + 66

        for offset in BATTERY_BASE_OFFSETS:
            address = rated_energy_base + offset
            try:
                words = await unit.read_holding_registers(address, 2)
            except ModbusExceptionError:
                break

            rated = decode_float32(words, word_order="little")
            if math.isnan(rated) or rated <= 0:
                break

            count += 1

        return count

    @staticmethod
    async def _block_present(unit: ModbusUnit, address: int) -> bool:
        """Return whether an optional control block answers a read.

        Rests on SolarEdge answering an absent block with a Modbus exception
        (illegal data address), not with zeros. A gateway that returns zeros for
        unmapped addresses would make every optional block look present.
        """
        try:
            await unit.read_holding_registers(address, 1)
        except ModbusExceptionError:
            return False
        return True
