"""Asynchronous Python client for SolarEdge inverters over Modbus."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from modbus_connection import ModbusError, ModbusExceptionError
from modbus_connection.decode import decode_float32
from modbus_connection.model import ComponentGroup
from modbus_connection.model.sunspec import SunSpecError, SunSpecModel, scan

from . import chain
from .components import (
    AdvancedPowerControl,
    Battery,
    Common,
    ExportControl,
    Inverter,
    InverterExtended,
    MeterDevice,
    Mmppt,
    PowerControl,
    StorageControl,
)
from .const import (
    ADVANCED_POWER_CONTROL_BASE,
    BATTERY_BASE_OFFSETS,
    BATTERY_COMMON_BASE,
    COMMON_MODEL_ID,
    EV_CHARGER_MODEL_PREFIX,
    EXPORT_CONTROL_BASE,
    GRID_STATUS_BASE,
    INVERTER_COMMON_BASE,
    INVERTER_MODEL_IDS,
    METER_DIDS,
    METER_MODEL_OFFSET,
    METER_SLOT_BASES,
    MMPPT_MODEL_ID,
    POWER_CONTROL_BASE,
    STORAGE_CONTROL_BASE,
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
        models: dict[int, list[SunSpecModel]],
        *,
        batteries: int = 0,
        grid_status: bool = False,
        storage_control: bool = False,
        export_control: bool = False,
        power_control: bool = False,
        advanced_power_control: bool = False,
    ) -> None:
        """Set up the components for the discovered model chain."""
        if not 0 <= batteries <= len(BATTERY_BASE_OFFSETS):
            msg = (
                f"batteries must be between 0 and {len(BATTERY_BASE_OFFSETS)}, "
                f"got {batteries}"
            )
            raise SolarEdgeError(msg)

        common_model = chain.first(models, COMMON_MODEL_ID)
        inverter_model = chain.first(models, *INVERTER_MODEL_IDS)
        if common_model is None or inverter_model is None:
            msg = "Device is not a SolarEdge inverter (no inverter model in the chain)"
            raise SolarEdgeError(msg)

        self._unit = unit

        # SolarEdge's grid status extension reuses registers inside the standard
        # inverter model rather than lengthening it, so the chain cannot report
        # it and the block is probed like the other proprietary ones.
        self.common = Common(unit, common_model)
        inverter_class = InverterExtended if grid_status else Inverter
        self.inverter: Inverter = inverter_class(unit, inverter_model)

        mppt_model = chain.first(models, MMPPT_MODEL_ID)
        self.mmppt = Mmppt(unit, mppt_model) if mppt_model is not None else None

        # SolarEdge keeps meter n at a fixed slot, shifted along by the whole
        # multiple-MPPT model when the inverter publishes one. The chain reports
        # that model's real span, so the shift comes from the device.
        shift = chain.span(mppt_model) if mppt_model is not None else 0
        self.meters: list[MeterDevice] = []
        for base in METER_SLOT_BASES:
            meter_common = chain.at(models, base + shift)
            meter_model = chain.at(models, base + shift + METER_MODEL_OFFSET)
            if meter_common is None or meter_common.model_id != COMMON_MODEL_ID:
                continue
            if meter_model is None or meter_model.model_id not in METER_DIDS:
                continue
            self.meters.append(MeterDevice(unit, meter_common, meter_model))

        self.batteries: list[Battery] = [
            Battery(unit, base_offset=BATTERY_BASE_OFFSETS[i]) for i in range(batteries)
        ]

        # Optional writable control blocks, outside the SunSpec chain.
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

        for meter in self.meters:
            parts.extend(meter.components)
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
        except SunSpecError as err:
            # A block whose header no longer matches the discovered model: the
            # map moved under us, so the values read are not what they claim.
            raise SolarEdgeError(str(err)) from err

    @classmethod
    async def async_probe(cls, unit: ModbusUnit) -> SolarEdge:
        """Discover the device on ``unit`` and return a ready instance.

        Walks the SunSpec model chain for the inverter, its identity, the
        multiple-MPPT extension and the meters, then probes the SolarEdge
        proprietary blocks, which are not part of the chain.
        """
        try:
            models = await scan(unit, INVERTER_COMMON_BASE)
        except SunSpecError as err:
            raise SolarEdgeError(str(err)) from err
        except ModbusError as err:
            raise SolarEdgeConnectionError(str(err)) from err

        try:
            return cls(
                unit,
                models,
                batteries=await cls._count_batteries(unit),
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
