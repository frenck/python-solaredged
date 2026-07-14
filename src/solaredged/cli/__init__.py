"""Command-line interface for SolarEdge over Modbus."""

from __future__ import annotations

import contextlib
import json
import sys
from typing import TYPE_CHECKING, Annotated

import typer
from modbus_connection import ModbusError, ModbusExceptionError
from modbus_connection.tmodbus import connect_tcp
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from solaredged.const import (
    InverterStatus,
    StorageChargePolicy,
    StorageControlMode,
    StorageMode,
)
from solaredged.exceptions import SolarEdgeConnectionError, SolarEdgeError
from solaredged.solaredged import SolarEdge

from ._sanitize import safe as _safe
from .async_typer import AsyncTyper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from enum import Enum

    from solaredged.components import Component, StorageControl

cli = AsyncTyper(
    help="SolarEdge Modbus CLI. Run without a command to launch the live TUI.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()

Host = Annotated[
    str,
    typer.Option(
        help="Hostname or IP address of the inverter",
        prompt="Host",
        show_default=False,
        envvar="SOLAREDGED_HOST",
    ),
]
Port = Annotated[
    int,
    typer.Option(help="Modbus TCP port", envvar="SOLAREDGED_PORT"),
]
Unit = Annotated[
    int,
    typer.Option(help="Modbus unit id of the inverter", envvar="SOLAREDGED_UNIT"),
]
JsonFlag = Annotated[
    bool,
    typer.Option("--json", help="Emit machine-readable JSON output"),
]
Yes = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Skip the confirmation prompt before writing"),
]
Timeout = Annotated[
    int,
    typer.Option(min=0, help="Remote-command timeout in seconds (0 = no timeout)"),
]


def _confirm_write(action: str, host: str, *, yes: bool) -> None:
    """Confirm a write to the device, unless ``--yes`` was given.

    Writes change live inverter registers, so every write command asks first.
    Aborting raises ``typer.Abort`` (exit code 1) and nothing is written.
    """
    if yes:
        return
    typer.confirm(f"Write {action} to {host}?", abort=True)


# Register blocks worth capturing for a raw dump (inverter + control blocks).
_DUMP_BLOCKS: tuple[tuple[int, int], ...] = (
    (40000, 69),
    (40069, 40),
    (57344, 18),
    (61440, 4),
)

_STATUS_ICONS: dict[InverterStatus, str] = {
    InverterStatus.OFF: "[dim]⭘[/dim]",
    InverterStatus.SLEEPING: "[dim]\U0001f4a4[/dim]",
    InverterStatus.STARTING: "[yellow]▲[/yellow]",
    InverterStatus.PRODUCING: "[green]☀️[/green]",
    InverterStatus.THROTTLED: "[yellow]☀️[/yellow]",
    InverterStatus.SHUTTING_DOWN: "[cyan]▼[/cyan]",
    InverterStatus.FAULT: "[red]❌[/red]",
    InverterStatus.STANDBY: "[dim]\U0001f527[/dim]",
}


@contextlib.asynccontextmanager
async def _client(host: str, port: int, unit: int) -> AsyncIterator[SolarEdge]:
    """Connect, probe and refresh a client, closing the connection after use."""
    try:
        conn = await connect_tcp(host, port=port)
    except ModbusError as err:
        # A failed connect (wrong host, closed port, Modbus TCP off) raises the
        # backend error before probing; wrap it so it reaches the error handler.
        raise SolarEdgeConnectionError(str(err)) from err
    try:
        # tmodbus's concrete unit type is not seen as the ModbusUnit protocol.
        client = await SolarEdge.async_probe(conn.for_unit(unit))  # ty: ignore[invalid-argument-type]
        await client.async_update()
        yield client
    finally:
        await conn.close()


def _require_storage(client: SolarEdge) -> StorageControl:
    """Return the storage control block or exit with a friendly message."""
    if client.storage_control is None:
        console.print("[red]This inverter has no storage (battery) control.[/red]")
        raise typer.Exit(code=1)
    return client.storage_control


async def _remote_control(
    storage: StorageControl,
    mode: StorageMode,
    limit_field: str,
    watts: float,
    timeout: int,  # noqa: ASYNC109  (device command timeout, not asyncio)
) -> None:
    """Drive the battery via remote control, engaging REMOTE_CONTROL last.

    The command mode, power limit and timeout are staged first; the control
    mode flips to REMOTE_CONTROL only once they are all set, so a failure
    partway through never leaves the inverter in remote control without its
    guard rails. On failure it points the caller at how to recover.
    """
    try:
        # Stage the parameters first.
        await storage.write("command_mode", mode)
        await storage.write(limit_field, watts)
        await storage.write("command_timeout", timeout)

        # Engage remote control last: if any staged write above failed, the
        # battery is never left in remote control without its limit and timeout.
        await storage.write("control_mode", StorageControlMode.REMOTE_CONTROL)
    except SolarEdgeConnectionError:
        console.print(
            "[yellow]Write failed partway through. If the inverter is in remote\n"
            "control, restore it with: "
            "storage-mode MAXIMIZE_SELF_CONSUMPTION[/yellow]"
        )
        raise


@cli.callback(invoke_without_command=True)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
def main_callback(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option(help="Hostname or IP (for TUI mode)", envvar="SOLAREDGED_HOST"),
    ] = "",
    port: Annotated[
        int, typer.Option(help="Modbus TCP port", envvar="SOLAREDGED_PORT")
    ] = 1502,
    unit: Annotated[
        int, typer.Option(help="Modbus unit id", envvar="SOLAREDGED_UNIT")
    ] = 1,
) -> None:
    """Launch the live TUI when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    if not host:  # pragma: no cover
        console.print(
            "[red]A host is required.[/red]\n"
            "Use [bold]--host[/bold] or set SOLAREDGED_HOST.\n"
            "\nRun [bold]solaredged --help[/bold] for all commands."
        )
        raise typer.Exit(code=1)
    try:  # pragma: no cover  # only reached when launching the TUI
        from solaredged.cli.tui import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            SolarEdgeTuiApp,
        )
    except ModuleNotFoundError as err:  # pragma: no cover
        # textual / textual_plotext live in the 'cli' extra and are imported
        # lazily here, so a partial install only fails when the TUI launches.
        console.print(
            "[red]The live TUI requires the 'cli' extra.[/red]\n"
            "Install it with: pip install 'solaredged[cli]'"
        )
        raise typer.Exit(code=1) from err

    SolarEdgeTuiApp(host=host, port=port, unit=unit).run()  # pragma: no cover
    raise typer.Exit  # pragma: no cover


@cli.error_handler(SolarEdgeConnectionError)
def connection_error_handler(_: SolarEdgeConnectionError) -> None:
    """Handle connection errors."""
    console.print(
        Panel(
            "Could not reach the inverter over Modbus. Check the host, port and\n"
            "that Modbus TCP is enabled on the device.",
            expand=False,
            title="Connection error",
            border_style="red bold",
        )
    )
    sys.exit(1)


@cli.error_handler(SolarEdgeError)
def solaredge_error_handler(err: SolarEdgeError) -> None:
    """Handle generic SolarEdge errors."""
    console.print(f"[red]❌ {err}[/red]")
    sys.exit(1)


def _status_display(status: InverterStatus | None) -> str:
    """Return a formatted status string with icon and label."""
    if status is None:
        return "❓ Unknown"
    icon = _STATUS_ICONS.get(status, "❓")
    return f"{icon}  {status.name.replace('_', ' ').title()}"


def _decoded(component: Component) -> dict[str, object]:
    """Return a component's decoded fields as a plain dict, for JSON output."""
    # pylint: disable-next=protected-access
    fields = type(component)._register_fields  # noqa: SLF001
    return {name: getattr(component, name) for name in sorted(fields)}


def _fmt(value: object, unit: str = "") -> str:
    """Format a value with an optional unit, or a dash when None."""
    if value is None:
        return "[dim]—[/dim]"
    return f"{value} {unit}".strip()


def _field_table() -> Table:
    """Build a borderless two-column key/value table."""
    table = Table(show_header=False, box=None, padding=(0, 2), min_width=44)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    return table


def _grid_display(on_grid: bool | None) -> str:
    """Render the grid on/off status."""
    if on_grid is None:
        return _fmt(None)
    return "[green]On-grid[/green]" if on_grid else "[yellow]Off-grid[/yellow]"


def _enum_label(member: Enum | None, *, fallback: str = "[dim]—[/dim]") -> str:
    """Return a friendly label for an enum member (e.g. 'Maximize Self Consumption')."""
    if member is None:
        return fallback
    return member.name.replace("_", " ").title()


def _on_off(value: bool | None) -> str:
    """Render a boolean flag as On/Off, or a dash when unknown."""
    if value is None:
        return _fmt(None)
    return "On" if value else "Off"


def _render_inverter(client: SolarEdge) -> None:
    """Render the inverter panel."""
    inv = client.inverter
    table = _field_table()
    table.add_row("\U0001f50c Status", _status_display(inv.status))
    table.add_row("\U0001f50c Grid", _grid_display(inv.on_grid))
    table.add_row("⚡ AC power", _fmt(inv.ac_power, "W"))
    table.add_row("\U0001f4c8 DC power", _fmt(inv.dc_power, "W"))
    table.add_row(
        "\U0001f50c AC",
        f"{_fmt(inv.ac_voltage_an, 'V')}  ·  {_fmt(inv.ac_current, 'A')}",
    )
    table.add_row("\U0001f504 Frequency", _fmt(inv.ac_frequency, "Hz"))
    table.add_row("\U0001f50b Lifetime", _fmt(inv.ac_energy, "Wh"))
    if inv.temperature_heatsink is not None:
        table.add_row("\U0001f321️  Heatsink", _fmt(inv.temperature_heatsink, "°C"))
    console.print(Panel(table, title="Inverter", border_style="green", expand=False))


def _render_strings(client: SolarEdge) -> None:
    """Render the per-string DC panel for a multiple-MPPT inverter."""
    if client.mmppt is None or not client.mmppt.modules:
        return
    table = Table(box=None, padding=(0, 2))
    for column in ("String", "Power", "Voltage", "Current", "Temp"):
        table.add_column(column, style="bold" if column == "String" else "")
    for module in client.mmppt.modules:
        table.add_row(
            _safe(module.label) if module.label else f"#{module.module_id}",
            _fmt(module.dc_power, "W"),
            _fmt(module.dc_voltage, "V"),
            _fmt(module.dc_current, "A"),
            _fmt(module.temperature, "°C"),
        )
    console.print(Panel(table, title="DC strings", border_style="yellow", expand=False))


def _render_meters(client: SolarEdge) -> None:
    """Render a panel per meter."""
    for index, meter in enumerate(client.meters, 1):
        table = _field_table()
        table.add_row("⚡ Power", _fmt(meter.ac_power, "W"))
        table.add_row("\U0001f504 Frequency", _fmt(meter.ac_frequency, "Hz"))
        table.add_row("\U0001f4e4 Exported", _fmt(meter.energy_exported, "Wh"))
        table.add_row("\U0001f4e5 Imported", _fmt(meter.energy_imported, "Wh"))
        console.print(
            Panel(table, title=f"Meter {index}", border_style="cyan", expand=False)
        )


def _render_batteries(client: SolarEdge) -> None:
    """Render a panel per battery."""
    for index, battery in enumerate(client.batteries, 1):
        status = (
            battery.status.name.title() if battery.status is not None else "Unknown"
        )
        table = _field_table()
        table.add_row("\U0001f50b Charge", _fmt(battery.state_of_energy, "%"))
        table.add_row("❤️  Health", _fmt(battery.state_of_health, "%"))
        table.add_row("⚡ Power", _fmt(battery.dc_power, "W"))
        table.add_row("\U0001f4a1 Status", status)
        table.add_row("\U0001f50b Available", _fmt(battery.energy_available, "Wh"))
        table.add_row("\U0001f321️  Temp", _fmt(battery.temperature_average, "°C"))
        console.print(
            Panel(table, title=f"Battery {index}", border_style="magenta", expand=False)
        )


def _render_controls(client: SolarEdge) -> None:
    """Render the writable control state, when present."""
    rows: list[tuple[str, str]] = []

    if (power := client.power_control) is not None:
        rows.append(("⚡ Power limit", _fmt(power.active_power_limit, "%")))
        rows.append(("\U0001f4d0 Cos φ", _fmt(power.cos_phi)))

    if (export := client.export_control) is not None:
        rows.append(
            ("\U0001f4e4 Export mode", _enum_label(export.mode, fallback="Disabled"))
        )
        rows.append(("\U0001f4e4 Export limit", _fmt(export.site_limit, "W")))
        rows.append(
            ("\U0001f4e4 Ext. prod. max", _fmt(export.external_production_max, "W"))
        )

    if (advanced := client.advanced_power_control) is not None:
        rows.append(
            ("\U0001f527 Reactive mode", _enum_label(advanced.reactive_power_config))
        )
        rows.append(("\U0001f527 Adv. power ctrl", _on_off(advanced.enabled)))

    if (storage := client.storage_control) is not None:
        rows.append(("\U0001f50b Storage mode", _enum_label(storage.control_mode)))
        rows.append(("\U0001f6e1️  Backup reserve", _fmt(storage.backup_reserve, "%")))

    if not rows:
        return

    table = _field_table()
    for label, value in rows:
        table.add_row(label, value)

    console.print(Panel(table, title="Controls", border_style="blue", expand=False))


def _render(client: SolarEdge) -> None:
    """Render the full device overview."""
    common = client.common
    title = f"{_safe(common.manufacturer or '')} {_safe(common.model or 'SolarEdge')}"
    console.print()
    console.print(f"  [bold]☀️  {title.strip()}[/bold]")
    console.print(
        f"  [dim]Serial {_safe(common.serial_number)} • "
        f"firmware {_safe(common.version)}[/dim]"
    )
    console.print()
    _render_inverter(client)
    _render_strings(client)
    _render_meters(client)
    _render_batteries(client)
    _render_controls(client)


@cli.command("info")
async def info_command(
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    output_json: JsonFlag = False,  # noqa: FBT002
) -> None:
    """Show inverter status, meters and batteries."""
    async with _client(host, port, unit) as client:
        if output_json:
            payload = {
                "common": _decoded(client.common),
                "inverter": _decoded(client.inverter),
                "mmppt": [_decoded(module) for module in client.mmppt.modules]
                if client.mmppt
                else [],
                "meters": [_decoded(meter) for meter in client.meters],
                "batteries": [_decoded(battery) for battery in client.batteries],
                "storage_control": _decoded(client.storage_control)
                if client.storage_control
                else None,
                "export_control": _decoded(client.export_control)
                if client.export_control
                else None,
                "power_control": _decoded(client.power_control)
                if client.power_control
                else None,
                "advanced_power_control": _decoded(client.advanced_power_control)
                if client.advanced_power_control
                else None,
            }
            typer.echo(json.dumps(payload, default=str, indent=2))
            return
        _render(client)


@cli.command("dump")
async def dump_command(
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
) -> None:
    """Dump raw register blocks as JSON (useful for debugging and fixtures)."""
    try:
        conn = await connect_tcp(host, port=port)
    except ModbusError as err:
        raise SolarEdgeConnectionError(str(err)) from err
    holding: dict[str, int] = {}
    try:
        modbus_unit = conn.for_unit(unit)
        for base, count in _DUMP_BLOCKS:
            try:
                regs = await modbus_unit.read_holding_registers(base, count)
            except ModbusExceptionError:
                # Block not implemented on this device; skip it. A transport
                # failure is a different error and propagates below.
                continue
            for offset, value in enumerate(regs):
                holding[str(base + offset)] = value
    except ModbusError as err:
        raise SolarEdgeConnectionError(str(err)) from err
    finally:
        await conn.close()
    if not holding:
        console.print("[red]No registers could be read from this device.[/red]")
        raise typer.Exit(code=1)
    typer.echo(json.dumps({"holding": holding}, indent=2))


PowerLimit = Annotated[
    int,
    typer.Argument(min=0, max=100, help="Active power limit as a percent of nominal"),
]


@cli.command("power-limit")
async def power_limit_command(
    level: PowerLimit,
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the inverter active power limit (percent of nominal)."""
    _confirm_write(f"active power limit {level}%", host, yes=yes)
    async with _client(host, port, unit) as client:
        if client.power_control is None:
            console.print("[red]This inverter has no power control block.[/red]")
            raise typer.Exit(code=1)
        await client.power_control.set_active_power_limit(level)
    console.print(f"⚡ [green]Active power limit set to {level}%.[/green]")


@cli.command("storage-mode")
async def storage_mode_command(
    mode: Annotated[str, typer.Argument(help="Storage control mode name")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the battery storage control mode.

    MODE is a name such as MAXIMIZE_SELF_CONSUMPTION, TIME_OF_USE, BACKUP_ONLY,
    REMOTE_CONTROL or DISABLED.
    """
    try:
        control_mode = StorageControlMode[mode.upper()]
    except KeyError:
        choices = ", ".join(mode.name for mode in StorageControlMode)
        console.print(f"[red]Unknown mode {mode!r}. Choose from: {choices}[/red]")
        raise typer.Exit(code=1) from None
    _confirm_write(f"storage mode {control_mode.name}", host, yes=yes)
    async with _client(host, port, unit) as client:
        if client.storage_control is None:
            console.print("[red]This inverter has no storage control block.[/red]")
            raise typer.Exit(code=1)
        await client.storage_control.set_control_mode(control_mode)
    console.print(f"\U0001f50b [green]Storage mode set to {control_mode.name}.[/green]")


@cli.command("cos-phi")
async def cos_phi_command(
    value: Annotated[
        float, typer.Argument(min=-1.0, max=1.0, help="Power factor setpoint")
    ],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the inverter power factor (cos phi) setpoint (-1.0 to 1.0)."""
    _confirm_write(f"power factor {value}", host, yes=yes)
    async with _client(host, port, unit) as client:
        if client.power_control is None:
            console.print("[red]This inverter has no power control block.[/red]")
            raise typer.Exit(code=1)
        await client.power_control.set_cos_phi(value)
    console.print(f"\U0001f4d0 [green]Power factor set to {value}.[/green]")


@cli.command("backup-reserve")
async def backup_reserve_command(
    percent: Annotated[float, typer.Argument(min=0, max=100, help="Reserved SoC %")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the battery backup-reserve level (percent of capacity)."""
    _confirm_write(f"backup reserve {percent}%", host, yes=yes)
    async with _client(host, port, unit) as client:
        await _require_storage(client).set_backup_reserve(percent)
    console.print(f"\U0001f6e1️  [green]Backup reserve set to {percent}%.[/green]")


@cli.command("charge-policy")
async def charge_policy_command(
    policy: Annotated[str, typer.Argument(help="AC charge policy name")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the AC charge policy (DISABLED, ALWAYS, FIXED_ENERGY_LIMIT, ...)."""
    try:
        value = StorageChargePolicy[policy.upper()]
    except KeyError:
        choices = ", ".join(policy.name for policy in StorageChargePolicy)
        console.print(f"[red]Unknown policy {policy!r}. Choose from: {choices}[/red]")
        raise typer.Exit(code=1) from None
    _confirm_write(f"AC charge policy {value.name}", host, yes=yes)
    async with _client(host, port, unit) as client:
        await _require_storage(client).set_ac_charge_policy(value)
    console.print(f"\U0001f50b [green]AC charge policy set to {value.name}.[/green]")


@cli.command("charge-limit")
async def charge_limit_command(
    watts: Annotated[float, typer.Argument(min=0, help="Remote charge power (W)")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the remote charge power limit (W)."""
    _confirm_write(f"charge limit {watts} W", host, yes=yes)
    async with _client(host, port, unit) as client:
        await _require_storage(client).set_charge_limit(watts)
    console.print(f"\U0001f50b [green]Charge limit set to {watts} W.[/green]")


@cli.command("discharge-limit")
async def discharge_limit_command(
    watts: Annotated[float, typer.Argument(min=0, help="Remote discharge power (W)")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Set the remote discharge power limit (W)."""
    _confirm_write(f"discharge limit {watts} W", host, yes=yes)
    async with _client(host, port, unit) as client:
        await _require_storage(client).set_discharge_limit(watts)
    console.print(f"\U0001f50b [green]Discharge limit set to {watts} W.[/green]")


@cli.command("remote-charge")
# pylint: disable-next=too-many-arguments, too-many-positional-arguments
async def remote_charge_command(  # noqa: PLR0913
    watts: Annotated[float, typer.Argument(min=0, help="Charge power (W)")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    timeout: Timeout = 3600,  # noqa: ASYNC109  (device command timeout, not asyncio)
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Actively charge the battery from solar and grid at the given power.

    Switches storage to remote control and applies a command timeout, after
    which the inverter reverts to its default mode.
    """
    _confirm_write(f"remote charge {watts} W for {timeout}s", host, yes=yes)
    async with _client(host, port, unit) as client:
        storage = _require_storage(client)
        # Stage the command parameters first and switch to REMOTE_CONTROL last,
        # so a mid-sequence failure never leaves the inverter in remote control
        # without its timeout and limit set.
        await _remote_control(
            storage,
            StorageMode.CHARGE_FROM_SOLAR_AND_GRID,
            "charge_limit",
            watts,
            timeout,
        )
    console.print(f"\U0001f50b [green]Charging at {watts} W for {timeout}s.[/green]")


@cli.command("remote-discharge")
# pylint: disable-next=too-many-arguments, too-many-positional-arguments
async def remote_discharge_command(  # noqa: PLR0913
    watts: Annotated[float, typer.Argument(min=0, help="Discharge power (W)")],
    host: Host,
    port: Port = 1502,
    unit: Unit = 1,
    timeout: Timeout = 3600,  # noqa: ASYNC109  (device command timeout, not asyncio)
    yes: Yes = False,  # noqa: FBT002
) -> None:
    """Actively discharge the battery to maximise export at the given power.

    Switches storage to remote control and applies a command timeout, after
    which the inverter reverts to its default mode.
    """
    _confirm_write(f"remote discharge {watts} W for {timeout}s", host, yes=yes)
    async with _client(host, port, unit) as client:
        storage = _require_storage(client)
        await _remote_control(
            storage,
            StorageMode.DISCHARGE_TO_MAXIMIZE_EXPORT,
            "discharge_limit",
            watts,
            timeout,
        )
    console.print(f"\U0001f50b [green]Discharging at {watts} W for {timeout}s.[/green]")
