"""Constants and enumerations for the SolarEdge Modbus interface.

Register addresses and layout follow the SunSpec information model as
implemented by SolarEdge. The base addresses below are the raw holding
register addresses used on the wire (the same numbers SolarEdge documents
and the ``solaredge-modbus-multi`` project reads).
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

# The SunSpec identifier ("SunS") found at the start of the common block.
SUNSPEC_ID = 0x53756E53

# SolarEdge EV chargers answer on their own unit id and present as an inverter,
# but expose no telemetry over Modbus. They are recognised by their model name.
EV_CHARGER_MODEL_PREFIX = "SE-EV-SA"

# -- block base addresses ------------------------------------------------------

# Inverter blocks (fixed).
INVERTER_COMMON_BASE = 40000
INVERTER_MODEL_BASE = 40069

# SolarEdge's grid status extension (grid on/off, extended vendor status). Not
# all firmware serves these registers, so their presence is probed.
GRID_STATUS_BASE = 40113

# Multiple-MPPT extension (SunSpec model 160). Shares base 40121 with meter 1
# when absent; when present it shifts the meter blocks up (not handled here).
MMPPT_BASE = 40121
MMPPT_UNITS_OFFSET = 8  # register offset of the module-count field (40129)

# Meter blocks. Meter 1 common starts here; each further meter is offset by a
# fixed stride. The model block sits right after the 67-register common block.
METER_COMMON_BASE = 40121
METER_MODEL_BASE = 40188
METER_STRIDE = 174
METER_COUNT = 3

# Battery blocks. The dynamic block sits after the 68-register common block.
# Battery spacing is not uniform, so each unit carries its own base offset.
BATTERY_COMMON_BASE = 57600
BATTERY_DYNAMIC_BASE = 57668
BATTERY_BASE_OFFSETS = (0, 256, 768)

# SolarEdge proprietary control blocks (word-swapped).
STORAGE_CONTROL_BASE = 57348
EXPORT_CONTROL_BASE = 57344
POWER_CONTROL_BASE = 61440
ADVANCED_POWER_CONTROL_BASE = 61696

# Bit positions within the export-control mode register (57344).
EXPORT_EXTERNAL_PRODUCTION_BIT = 10
EXPORT_NEGATIVE_SITE_LIMIT_BIT = 11


# -- enumerations --------------------------------------------------------------


class SunSpecDID(IntEnum):
    """SunSpec device identifiers used by SolarEdge devices."""

    SINGLE_PHASE_INVERTER = 101
    SPLIT_PHASE_INVERTER = 102
    THREE_PHASE_INVERTER = 103
    MULTIPLE_MPPT = 160
    SINGLE_PHASE_METER = 201
    SPLIT_PHASE_METER = 202
    THREE_PHASE_WYE_METER = 203
    THREE_PHASE_DELTA_METER = 204


class InverterStatus(IntEnum):
    """Inverter operating state (SunSpec ``I_Status``)."""

    OFF = 1
    SLEEPING = 2
    STARTING = 3
    PRODUCING = 4
    THROTTLED = 5
    SHUTTING_DOWN = 6
    FAULT = 7
    STANDBY = 8


class BatteryStatus(IntEnum):
    """Battery operating state (SolarEdge ``B_Status``)."""

    OFF = 0
    STANDBY = 1
    INIT = 2
    CHARGE = 3
    DISCHARGE = 4
    FAULT = 5
    PRESERVE_CHARGE = 6
    IDLE = 7
    POWER_SAVING = 10


class StorageControlMode(IntEnum):
    """Top-level storage control mode."""

    DISABLED = 0
    MAXIMIZE_SELF_CONSUMPTION = 1
    TIME_OF_USE = 2
    BACKUP_ONLY = 3
    REMOTE_CONTROL = 4


class StorageChargePolicy(IntEnum):
    """AC charge policy for the storage system."""

    DISABLED = 0
    ALWAYS = 1
    FIXED_ENERGY_LIMIT = 2
    PERCENT_OF_PRODUCTION = 3


class StorageMode(IntEnum):
    """Remote-control charge/discharge mode (default and command)."""

    SOLAR_ONLY = 0
    CHARGE_FROM_CLIPPED_SOLAR = 1
    CHARGE_FROM_SOLAR = 2
    CHARGE_FROM_SOLAR_AND_GRID = 3
    DISCHARGE_TO_MAXIMIZE_EXPORT = 4
    DISCHARGE_TO_MINIMIZE_IMPORT = 5
    MAXIMIZE_SELF_CONSUMPTION = 7


class ExportControlMode(IntEnum):
    """Site export / limit control mode."""

    EXPORT_CONTROL_EXPORT_IMPORT_METER = 0
    EXPORT_CONTROL_CONSUMPTION_METER = 1
    PRODUCTION_CONTROL = 2


class ExportControlLimit(IntEnum):
    """How the export limit is applied."""

    TOTAL = 0
    PER_PHASE = 1


class ReactivePowerConfig(IntEnum):
    """Reactive power configuration mode."""

    FIXED_COSPHI = 0
    FIXED_Q = 1
    COSPHI_P = 2
    QU_QP = 3
    RRCR = 4


class MeterEvent(IntFlag):
    """Meter event flags (``M_Events`` bitfield)."""

    POWER_FAILURE = 1 << 2
    UNDER_VOLTAGE = 1 << 3
    LOW_PF = 1 << 4
    OVER_CURRENT = 1 << 5
    OVER_VOLTAGE = 1 << 6
    MISSING_SENSOR = 1 << 7
