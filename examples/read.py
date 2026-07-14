"""Read live data from a SolarEdge inverter over Modbus.

The consumer owns the Modbus connection and hands the library a unit; a
multi-inverter site creates one ``SolarEdge`` per unit on the same connection.
"""

from __future__ import annotations

import asyncio

from modbus_connection.tmodbus import connect_tcp

from solaredged import SolarEdge


async def main() -> None:
    """Connect, probe the device layout, and print a reading."""
    connection = await connect_tcp("solaredge.local", port=1502)
    try:
        unit = connection.for_unit(1)
        # tmodbus's concrete unit type is not seen as the ModbusUnit protocol.
        solaredge = await SolarEdge.async_probe(unit)  # ty: ignore[invalid-argument-type]
        await solaredge.async_update()

        inverter = solaredge.inverter
        print(f"{solaredge.common.manufacturer} {solaredge.common.model}")
        print(f"  status:    {inverter.status.name if inverter.status else 'unknown'}")
        print(f"  AC power:  {inverter.ac_power} W")
        print(f"  DC power:  {inverter.dc_power} W")
        print(f"  lifetime:  {inverter.ac_energy} Wh")

        for index, meter in enumerate(solaredge.meters, 1):
            print(f"  meter {index}: {meter.ac_power} W")
        for index, battery in enumerate(solaredge.batteries, 1):
            print(f"  battery {index}: {battery.state_of_energy}% charged")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
