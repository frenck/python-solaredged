"""Write a setting to a SolarEdge inverter over Modbus.

Control blocks are optional: ``async_probe`` only wires them up when the device
exposes them. Each control component exposes typed ``set_*`` methods that
validate and encode the value before touching the device.

Only run this against hardware you own and understand: these registers change
how the inverter and battery behave.
"""

from __future__ import annotations

import asyncio

from modbus_connection.tmodbus import connect_tcp

from solaredged import SolarEdge, StorageControlMode


async def main() -> None:
    """Set the storage control mode, if the device supports storage control."""
    connection = await connect_tcp("solaredge.local", port=1502)
    try:
        # tmodbus's concrete unit type is not seen as the ModbusUnit protocol.
        unit = connection.for_unit(1)
        solaredge = await SolarEdge.async_probe(unit)  # ty: ignore[invalid-argument-type]
        if solaredge.storage_control is None:
            print("This inverter has no storage control block.")
            return

        await solaredge.storage_control.set_control_mode(
            StorageControlMode.MAXIMIZE_SELF_CONSUMPTION
        )
        print("Storage control mode set to maximize self consumption.")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
