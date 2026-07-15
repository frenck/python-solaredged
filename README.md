# Python: Asynchronous client for SolarEdge over Modbus

[![GitHub Release][releases-shield]][releases]
[![Python Versions][python-versions-shield]][pypi]
![Project Stage][project-stage-shield]
![Project Maintenance][maintenance-shield]
[![License][license-shield]](LICENSE.md)

[![Build Status][build-shield]][build]
[![Code Coverage][codecov-shield]][codecov]
[![OpenSSF Scorecard][scorecard-shield]][scorecard]
[![Open in Dev Containers][devcontainer-shield]][devcontainer]

[![Sponsor Frenck via GitHub Sponsors][github-sponsors-shield]][github-sponsors]

[![Support Frenck on Patreon][patreon-shield]][patreon]

Asynchronous Python client for SolarEdge inverters over Modbus.

## About

This package reads and controls SolarEdge inverters, meters and batteries over
Modbus, using the SunSpec register model. It is built for use inside
[Home Assistant](https://www.home-assistant.io), following the shared-connection
approach described in the [Modernizing Modbus][modbus-blog] developer blog.

It is built on top of [`modbus-connection`][modbus-connection], a
backend-neutral async Modbus toolkit. The library does **not** own the
connection: the consumer opens a `ModbusConnection` and hands over a
`ModbusUnit`. A site with several inverters creates one `SolarEdge` per unit,
all sharing a single connection.

Because the caller owns the connection, the transport is up to you. Modbus TCP
over Ethernet or WiFi is the common path, but the library works just as well
over RS485/RTU or an RTU-to-TCP gateway (such as an Elfin-EW11). Anything that
hands it a `ModbusUnit` will do; the register map does not care how the bytes
arrive.

Supported out of the box:

- Inverter: power, energy, per-phase voltage/current, frequency, DC, temperatures, status, grid on/off, vendor status
- Per-string DC (multiple-MPPT / SunSpec 160): current, voltage, power, energy, temperature per module
- Up to three meters: power, per-phase measurements, imported/exported energy, apparent (VAh) and reactive (varh) energy
- Batteries: state of energy/health, power, temperatures, status
- Control blocks: storage control, export/site limit + external production, dynamic power control, advanced/reactive power control
- EV charger detection (identity only; SolarEdge exposes no charger telemetry over Modbus)

## Enabling Modbus TCP on your inverter

Modbus TCP is off by default. Enable it once on the inverter, then point this
library at the inverter's IP address on port 1502.

Inverters with a display (older LCD models):

1. Long-press OK (the rightmost button) to enter the menu.
2. Enter the password by pressing Up, Down, OK, Up, Down, OK, Up, Down (`12312312`).
3. Go to Communication, select LAN, and enable Modbus TCP.

Inverters set up with SetApp (no display): open the SetApp interface (join the
inverter's WiFi access point and browse to `http://172.16.0.1`, or use the
SetApp mobile app), then under Site Communication enable Modbus (TCP).

SolarEdge may drop WiFi-based Modbus in future firmware, so a wired Ethernet
connection is the more reliable choice. Instructions adapted from the community
[home-assistant-solaredge-modbus][binsentsu] project.

## Installation

```bash
pip install solaredged
```

To install with the optional CLI, which drives the tmodbus backend:

```bash
pip install "solaredged[cli,tmodbus]"
```

As a library, `solaredged` talks to a `ModbusUnit`; pick a backend with the
matching extra, `solaredged[pymodbus]` or `solaredged[tmodbus]`, or let your
application supply one.

## Usage

The consumer owns the connection and hands the library a unit. The example
below uses the tmodbus backend, so install it with `pip install
"solaredged[tmodbus]"` first:

```python
import asyncio

from modbus_connection.tmodbus import connect_tcp

from solaredged import SolarEdge


async def main() -> None:
    connection = await connect_tcp("solaredge.local", port=1502)
    try:
        unit = connection.for_unit(1)
        solaredge = await SolarEdge.async_probe(unit)  # detects the layout
        await solaredge.async_update()                 # one pooled read

        print(solaredge.inverter.ac_power, "W")
        print(solaredge.inverter.status)
        for battery in solaredge.batteries:
            print(battery.state_of_energy, "%")
    finally:
        await connection.close()


asyncio.run(main())
```

`async_probe` validates the SunSpec header and detects which meters, batteries
and control blocks are present. `async_update` refreshes every component in as
few Modbus reads as possible. Values decode to `None` when the device reports a
point as not implemented, and a battery state of energy or health outside
0-100 (reported by initializing batteries) decodes to `None` as well.

See the [examples](examples) directory for more.

## CLI

The optional CLI reads your inverter straight from the terminal. `--host`,
`--port` and `--unit` can also be set via the `SOLAREDGED_HOST`,
`SOLAREDGED_PORT` and `SOLAREDGED_UNIT` environment variables.

```bash
# Launch the live TUI dashboard
solaredged --host solaredge.local

# Show a one-shot reading
solaredged info --host solaredge.local

# Machine-readable output
solaredged info --host solaredge.local --json

# Dump the raw register map (handy for debugging and fixtures)
solaredged dump --host solaredge.local
```

Control commands write to the inverter, so only use them on hardware you own.
Each one asks for confirmation before writing; pass `--yes` (or `-y`) to skip
the prompt in scripts.

```bash
# Limit active power output to a percentage of nominal
solaredged power-limit 80 --host solaredge.local

# Set the battery storage control mode
solaredged storage-mode MAXIMIZE_SELF_CONSUMPTION --host solaredge.local

# Set the power factor (cos phi) setpoint
solaredged cos-phi 0.95 --host solaredge.local

# Battery: reserve, charge policy, and remote charge/discharge
solaredged backup-reserve 20 --host solaredge.local
solaredged charge-policy ALWAYS --host solaredge.local
solaredged remote-charge 3000 --timeout 1800 --host solaredge.local
solaredged remote-discharge 4000 --host solaredge.local

# Skip the confirmation prompt (for scripting)
solaredged power-limit 80 --host solaredge.local --yes
```

More controls (export mode/limit, external production, advanced/reactive power
control, commit/restore) are available through the library API on the
`storage_control`, `export_control`, `power_control` and
`advanced_power_control` components.

## Changelog & releases

This repository keeps a change log using [GitHub's releases][releases]
functionality. Releases are based on [Semantic Versioning][semver], and use the
format of `MAJOR.MINOR.PATCH`.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](.github/CONTRIBUTING.md) for
how to get started and what the review expects.

## Setting up development environment

The easiest way to start, is by opening a CodeSpace here on GitHub, or by using
the [Dev Container][devcontainer] feature of Visual Studio Code.

[![Open in Dev Containers][devcontainer-shield]][devcontainer]

This Python project is fully managed using the [Poetry][poetry] dependency
manager, and relies on NodeJS for some of the checks during development.

You need at least:

- Python 3.12+
- [Poetry][poetry-install]
- NodeJS 24+ (including NPM)

To install all packages, including all development requirements:

```bash
npm install
poetry install
```

As this repository uses the [prek][prek] framework, all changes
are linted and tested with each commit. You can run all checks and tests
manually, using the following command:

```bash
poetry run prek run --all-files
```

To run just the Python tests:

```bash
poetry run pytest
```

## Authors & contributors

The original setup of this repository is by [Franck Nijhof][frenck].

For a full list of all authors and contributors,
check [the contributor's page][contributors].

## Disclaimer

This project is an independent, community-driven effort. It is **not
affiliated with, endorsed by, or supported by** SolarEdge Technologies. All
product names, trademarks, and registered trademarks are property of their
respective owners.

The register map is based on SolarEdge's publicly documented SunSpec
implementation and the excellent [`solaredge-modbus-multi`][solaredge-modbus-multi]
community integration. This work is done for interoperability purposes.

Use this software at your own risk. The authors are not responsible for any
damage to your equipment, property, or person resulting from the use of this
library. Writing control registers changes how your inverter and battery
behave; only do so on hardware you own and understand.

## License

MIT License

Copyright (c) 2026 Franck Nijhof

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

[binsentsu]: https://github.com/binsentsu/home-assistant-solaredge-modbus
[build-shield]: https://github.com/frenck/python-solaredged/actions/workflows/tests.yaml/badge.svg
[build]: https://github.com/frenck/python-solaredged/actions/workflows/tests.yaml
[codecov-shield]: https://codecov.io/gh/frenck/python-solaredged/branch/main/graph/badge.svg
[codecov]: https://codecov.io/gh/frenck/python-solaredged
[contributors]: https://github.com/frenck/python-solaredged/graphs/contributors
[devcontainer-shield]: https://img.shields.io/static/v1?label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode
[devcontainer]: https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/frenck/python-solaredged
[frenck]: https://github.com/frenck
[github-sponsors-shield]: https://frenck.dev/wp-content/uploads/2019/12/github_sponsor.png
[github-sponsors]: https://github.com/sponsors/frenck
[license-shield]: https://img.shields.io/github/license/frenck/python-solaredged.svg
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg
[modbus-blog]: https://developers.home-assistant.io/blog/2026/07/05/modernizing-modbus/
[modbus-connection]: https://home-assistant-libs.github.io/modbus-connection/
[patreon-shield]: https://frenck.dev/wp-content/uploads/2019/12/patreon.png
[patreon]: https://www.patreon.com/frenck
[poetry-install]: https://python-poetry.org/docs/#installation
[poetry]: https://python-poetry.org
[prek]: https://github.com/frenck/prek
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg
[pypi]: https://pypi.org/project/solaredged/
[python-versions-shield]: https://img.shields.io/pypi/pyversions/solaredged
[releases-shield]: https://img.shields.io/github/release/frenck/python-solaredged.svg
[releases]: https://github.com/frenck/python-solaredged/releases
[scorecard-shield]: https://api.scorecard.dev/projects/github.com/frenck/python-solaredged/badge
[scorecard]: https://scorecard.dev/viewer/?uri=github.com/frenck/python-solaredged
[semver]: http://semver.org/spec/v2.0.0.html
[solaredge-modbus-multi]: https://github.com/WillCodeForCats/solaredge-modbus-multi
