"""Live TUI dashboard for a SolarEdge inverter (monitoring + power limit)."""

# pylint: disable=import-error,too-few-public-methods
from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Label, Static
from textual_plotext import PlotextPlot

from modbus_connection.tmodbus import connect_tcp

from solaredged.cli._sanitize import safe as _safe
from solaredged.const import StorageControlMode
from solaredged.solaredged import SolarEdge

if TYPE_CHECKING:
    from modbus_connection import ModbusConnection

POLL_INTERVAL = 10
MAX_HISTORY = 120


class PowerLimitDialog(ModalScreen[int | None]):
    """Keyboard-only dialog for setting the active power limit (0-100%)."""

    CSS = """
    PowerLimitDialog { align: center middle; }
    .modal-box {
        width: 40; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    .modal-box Label { width: 100%; text-align: center; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "adjust(1)", show=False),
        Binding("down", "adjust(-1)", show=False),
        Binding("+", "adjust(5)", show=False),
        Binding("-", "adjust(-5)", show=False),
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, current: int) -> None:
        """Initialise with the current power limit."""
        super().__init__()
        self._level = current

    def _display(self) -> str:
        return f"[bold cyan]{self._level} %[/bold cyan]"

    def compose(self) -> ComposeResult:
        """Create the dialog layout."""
        with Vertical(classes="modal-box"):
            yield Label("⚡ [bold]Active Power Limit[/bold]")
            yield Label("")
            yield Label(self._display(), id="pl-value")
            yield Label("")
            yield Label("[dim]↑↓ ±1  +/- ±5 • Enter confirm • Esc cancel[/dim]")

    def action_adjust(self, delta: int) -> None:
        """Adjust the level, clamped to 0-100."""
        self._level = max(0, min(100, self._level + delta))
        self.query_one("#pl-value", Label).update(self._display())

    def action_confirm(self) -> None:
        """Confirm the selected level."""
        self.dismiss(self._level)

    def action_cancel(self) -> None:
        """Cancel without changing anything."""
        self.dismiss(None)


class CosPhiDialog(ModalScreen[float | None]):
    """Keyboard-only dialog for setting the power factor (-1.0 to 1.0)."""

    CSS = """
    CosPhiDialog { align: center middle; }
    .modal-box {
        width: 40; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    .modal-box Label { width: 100%; text-align: center; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "adjust(0.1)", show=False),
        Binding("down", "adjust(-0.1)", show=False),
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, current: float) -> None:
        """Initialise with the current power factor."""
        super().__init__()
        self._value = current

    def _display(self) -> str:
        return f"[bold cyan]{self._value:.1f}[/bold cyan]"

    def compose(self) -> ComposeResult:
        """Create the dialog layout."""
        with Vertical(classes="modal-box"):
            yield Label("📐 [bold]Power Factor (cos φ)[/bold]")
            yield Label("")
            yield Label(self._display(), id="cp-value")
            yield Label("")
            yield Label("[dim]↑↓ ±0.1 • Enter confirm • Esc cancel[/dim]")

    def action_adjust(self, delta: float) -> None:
        """Adjust the power factor, clamped to -1.0..1.0."""
        self._value = round(max(-1.0, min(1.0, self._value + delta)), 1)
        self.query_one("#cp-value", Label).update(self._display())

    def action_confirm(self) -> None:
        """Confirm the selected value."""
        self.dismiss(self._value)

    def action_cancel(self) -> None:
        """Cancel without changing anything."""
        self.dismiss(None)


class BackupReserveDialog(ModalScreen[float | None]):
    """Keyboard-only dialog for setting the backup reserve (0-100%)."""

    CSS = """
    BackupReserveDialog { align: center middle; }
    .modal-box {
        width: 40; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    .modal-box Label { width: 100%; text-align: center; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "adjust(1)", show=False),
        Binding("down", "adjust(-1)", show=False),
        Binding("+", "adjust(5)", show=False),
        Binding("-", "adjust(-5)", show=False),
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    def __init__(self, current: float) -> None:
        """Initialise with the current backup reserve."""
        super().__init__()
        self._value = current

    def _display(self) -> str:
        return f"[bold cyan]{self._value:.0f} %[/bold cyan]"

    def compose(self) -> ComposeResult:
        """Create the dialog layout."""
        with Vertical(classes="modal-box"):
            yield Label("🛡️  [bold]Backup Reserve[/bold]")
            yield Label("")
            yield Label(self._display(), id="br-value")
            yield Label("")
            yield Label("[dim]↑↓ ±1  +/- ±5 • Enter confirm • Esc cancel[/dim]")

    def action_adjust(self, delta: float) -> None:
        """Adjust the reserve, clamped to 0-100."""
        self._value = max(0.0, min(100.0, self._value + delta))
        self.query_one("#br-value", Label).update(self._display())

    def action_confirm(self) -> None:
        """Confirm the selected value."""
        self.dismiss(self._value)

    def action_cancel(self) -> None:
        """Cancel without changing anything."""
        self.dismiss(None)


class StorageModeDialog(ModalScreen[StorageControlMode | None]):
    """Keyboard-only picker for the battery storage control mode."""

    CSS = """
    StorageModeDialog { align: center middle; }
    .modal-box {
        width: 44; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    .modal-box Label { width: 100%; text-align: center; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "adjust(-1)", show=False),
        Binding("down", "adjust(1)", show=False),
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm"),
    ]

    _MODES: ClassVar[list[StorageControlMode]] = list(StorageControlMode)

    def __init__(self, current: StorageControlMode | None) -> None:
        """Initialise on the current mode."""
        super().__init__()
        self._index = self._MODES.index(current) if current in self._MODES else 0

    def _display(self) -> str:
        name = self._MODES[self._index].name.replace("_", " ").title()
        return f"[bold cyan]{name}[/bold cyan]"

    def compose(self) -> ComposeResult:
        """Create the dialog layout."""
        with Vertical(classes="modal-box"):
            yield Label("🔋 [bold]Storage Mode[/bold]")
            yield Label("")
            yield Label(self._display(), id="sm-value")
            yield Label("")
            yield Label("[dim]↑↓ change • Enter confirm • Esc cancel[/dim]")

    def action_adjust(self, delta: int) -> None:
        """Move through the available modes."""
        self._index = (self._index + delta) % len(self._MODES)
        self.query_one("#sm-value", Label).update(self._display())

    def action_confirm(self) -> None:
        """Confirm the selected mode."""
        self.dismiss(self._MODES[self._index])

    def action_cancel(self) -> None:
        """Cancel without changing anything."""
        self.dismiss(None)


class StatusWidget(Static):
    """Displays the current inverter status."""

    def update_client(self, client: SolarEdge) -> None:
        """Update the display from a refreshed client."""
        inv = client.inverter
        status = inv.status.name.replace("_", " ").title() if inv.status else "Unknown"
        grid = "on-grid" if inv.on_grid else "off-grid" if inv.on_grid is False else "—"
        lines = [
            f"  [bold]{status}[/bold]  [dim]({grid})[/dim]",
            "",
            f"  ⚡ AC power      {inv.ac_power} W",
            f"  \U0001f4c8 DC power      {inv.dc_power} W",
            f"  \U0001f50b Lifetime      {inv.ac_energy} Wh",
            f"  \U0001f504 Frequency     {inv.ac_frequency} Hz",
        ]
        if inv.temperature_heatsink is not None:
            lines.append(f"  \U0001f321️  Heatsink      {inv.temperature_heatsink} °C")
        for index, battery in enumerate(client.batteries, 1):
            lines.append(
                f"  \U0001f50b Battery {index}     {battery.state_of_energy} % "
                f"({battery.dc_power} W)"
            )
        self.update("\n".join(lines))


class InfoWidget(Static):
    """Displays device identity."""

    def update_client(self, client: SolarEdge) -> None:
        """Update the display from a refreshed client."""
        common = client.common
        title = (
            f"{_safe(common.manufacturer or '')} {_safe(common.model or '')}".strip()
        )
        strings = len(client.mmppt.modules) if client.mmppt else 0
        lines = [
            f"  [bold]☀️  {title}[/bold]",
            f"  [dim]{_safe(common.serial_number)}[/dim]",
            "",
            f"  Firmware   {_safe(common.version)}",
            f"  Strings    {strings}",
            f"  Meters     {len(client.meters)}",
            f"  Batteries  {len(client.batteries)}",
        ]
        if client.power_control and client.power_control.active_power_limit is not None:
            lines.append(f"  Pwr limit  {client.power_control.active_power_limit} %")
        if client.storage_control and client.storage_control.control_mode is not None:
            mode = client.storage_control.control_mode.name.replace("_", " ").title()
            lines.append(f"  Storage    {mode}")
        self.update("\n".join(lines))


class SolarEdgeTuiApp(App[None]):
    """Live monitoring dashboard for a SolarEdge inverter."""

    CSS = """
    Screen { layout: vertical; }
    #top { layout: horizontal; height: 11; }
    #status-panel { width: 1fr; border: round green; padding: 1; }
    #info-panel { width: 1fr; border: round $primary-lighten-2; padding: 1; }
    #power-graph { height: 1fr; min-height: 8; border: round cyan; }
    #power-plot { height: 1fr; }
    #status-bar {
        dock: bottom; height: 1; background: $surface; color: $text-muted; padding: 0 1;
    }
    """

    TITLE = "☀️  SolarEdge"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("l", "power_limit", "Power limit"),
        Binding("c", "cos_phi", "Cos φ"),
        Binding("b", "backup_reserve", "Backup %"),
        Binding("s", "storage_mode", "Storage"),
    ]

    def __init__(self, host: str, port: int = 1502, unit: int = 1) -> None:
        """Initialize the app."""
        super().__init__()
        self._host = host
        self._port = port
        self._unit = unit
        self._connection: ModbusConnection | None = None
        self._client: SolarEdge | None = None
        self._power_history: deque[float] = deque(maxlen=MAX_HISTORY)
        self._poll_count = 0
        # Serialises every Modbus operation: the periodic poll and the write
        # workers run in separate worker groups (so a poll cannot cancel a
        # write), so this keeps their requests off the wire at the same time.
        self._modbus_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        """Create the layout."""
        yield Header()
        with Horizontal(id="top"):
            with Container(id="status-panel"):
                yield Label("[bold]Status[/bold]")
                yield StatusWidget(id="status")
            with Container(id="info-panel"):
                yield Label("[bold]Device[/bold]")
                yield InfoWidget(id="info")
        with Container(id="power-graph"):
            yield PlotextPlot(id="power-plot")
        yield Label("  Connecting…", id="status-bar")
        yield Footer()

    def _update_graph(self) -> None:
        """Redraw the AC power graph."""
        widget = self.query_one("#power-plot", PlotextPlot)
        plt = widget.plt
        plt.clear_figure()
        plt.theme("dark")
        plt.title("⚡ AC Power")
        plt.ylabel("W")
        if self._power_history:
            x = list(range(len(self._power_history)))
            plt.plot(
                x,
                list(self._power_history),
                label=f"{self._power_history[-1]} W",
                color="green",
            )
        widget.refresh()

    def on_mount(self) -> None:
        """Start polling on mount."""
        self._poll()
        self.set_interval(POLL_INTERVAL, self._poll)

    @work(exclusive=True, group="poll")
    async def _poll(self) -> None:
        """Fetch fresh data from the inverter."""
        status_bar = self.query_one("#status-bar", Label)
        try:
            async with self._modbus_lock:
                if self._client is None:
                    connection = await connect_tcp(self._host, port=self._port)
                    probed = False
                    try:
                        # tmodbus's unit type is not the ModbusUnit protocol.
                        client = await SolarEdge.async_probe(
                            connection.for_unit(self._unit)  # ty: ignore[invalid-argument-type]
                        )
                        probed = True
                    finally:
                        # A failed probe must not orphan the open connection, or
                        # each retry would leak another socket.
                        if not probed:
                            await connection.close()

                    # Probe succeeded: take ownership so on_unmount can close it.
                    self._connection = connection  # ty: ignore[invalid-assignment]
                    self._client = client

                await self._client.async_update()

            self.query_one("#status", StatusWidget).update_client(self._client)
            self.query_one("#info", InfoWidget).update_client(self._client)

            if (power := self._client.inverter.ac_power) is not None:
                self._power_history.append(power)
            self._update_graph()

            self._poll_count += 1
            status_bar.update(
                f"  Poll #{self._poll_count} • every {POLL_INTERVAL}s"
                "  •  [dim]l[/dim] limit  [dim]c[/dim] cosφ  [dim]b[/dim] backup"
                "  [dim]s[/dim] storage  [dim]r[/dim] refresh  [dim]q[/dim] quit"
            )
        except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            status_bar.update(f"  [red]⚠️  {err}[/red]")

    def action_refresh(self) -> None:
        """Manual refresh."""
        self._poll()

    def action_power_limit(self) -> None:
        """Open the power-limit dialog, if the inverter supports it."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.power_control is None:
            status_bar.update("  [yellow]No power control on this inverter.[/yellow]")
            return
        current = self._client.power_control.active_power_limit
        current = 100 if current is None else current

        def _on_dismiss(level: int | None) -> None:
            if level is not None:
                self._set_power_limit(level)

        self.push_screen(PowerLimitDialog(current), _on_dismiss)

    @work(exclusive=True, group="write")
    async def _set_power_limit(self, level: int) -> None:
        """Write the active power limit and refresh."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.power_control is None:
            return

        try:
            async with self._modbus_lock:
                await self._client.power_control.write("active_power_limit", level)
        except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            status_bar.update(f"  [red]⚠️  {err}[/red]")
            return
        status_bar.update(f"  [green]✓ Power limit set to {level}%[/green]")
        self._poll()

    def action_cos_phi(self) -> None:
        """Open the power-factor dialog, if the inverter supports it."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.power_control is None:
            status_bar.update("  [yellow]No power control on this inverter.[/yellow]")
            return
        current = self._client.power_control.cos_phi
        current = 1.0 if current is None else current

        def _on_dismiss(value: float | None) -> None:
            if value is not None:
                self._set_cos_phi(value)

        self.push_screen(CosPhiDialog(current), _on_dismiss)

    @work(exclusive=True, group="write")
    async def _set_cos_phi(self, value: float) -> None:
        """Write the power factor and refresh."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.power_control is None:
            return

        try:
            async with self._modbus_lock:
                await self._client.power_control.write("cos_phi", value)
        except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            status_bar.update(f"  [red]⚠️  {err}[/red]")
            return
        status_bar.update(f"  [green]✓ Cos φ set to {value}[/green]")
        self._poll()

    def action_backup_reserve(self) -> None:
        """Open the backup-reserve dialog, if the inverter has storage control."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.storage_control is None:
            status_bar.update("  [yellow]No storage control on this inverter.[/yellow]")
            return
        current = self._client.storage_control.backup_reserve
        current = 0.0 if current is None else current

        def _on_dismiss(value: float | None) -> None:
            if value is not None:
                self._set_backup_reserve(value)

        self.push_screen(BackupReserveDialog(current), _on_dismiss)

    @work(exclusive=True, group="write")
    async def _set_backup_reserve(self, value: float) -> None:
        """Write the backup reserve and refresh."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.storage_control is None:
            return

        try:
            async with self._modbus_lock:
                await self._client.storage_control.write("backup_reserve", value)
        except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            status_bar.update(f"  [red]⚠️  {err}[/red]")
            return
        status_bar.update(f"  [green]✓ Backup reserve set to {value:.0f}%[/green]")
        self._poll()

    def action_storage_mode(self) -> None:
        """Open the storage-mode picker, if the inverter has storage control."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.storage_control is None:
            status_bar.update("  [yellow]No storage control on this inverter.[/yellow]")
            return
        current = self._client.storage_control.control_mode

        def _on_dismiss(mode: StorageControlMode | None) -> None:
            if mode is not None:
                self._set_storage_mode(mode)

        self.push_screen(StorageModeDialog(current), _on_dismiss)

    @work(exclusive=True, group="write")
    async def _set_storage_mode(self, mode: StorageControlMode) -> None:
        """Write the storage control mode and refresh."""
        status_bar = self.query_one("#status-bar", Label)
        if self._client is None or self._client.storage_control is None:
            return

        try:
            async with self._modbus_lock:
                await self._client.storage_control.write("control_mode", mode)
        except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            status_bar.update(f"  [red]⚠️  {err}[/red]")
            return
        status_bar.update(f"  [green]✓ Storage mode: {mode.name}[/green]")
        self._poll()

    async def on_unmount(self) -> None:
        """Close the connection when the app exits.

        Stop polling first, then let any in-flight write land before the
        connection goes away, so quitting mid-write cannot leave a register
        half-written.
        """
        self.workers.cancel_group(self, "poll")
        write_workers = [worker for worker in self.workers if worker.group == "write"]
        if write_workers:
            await self.workers.wait_for_complete(write_workers)
        if self._connection is not None:
            await self._connection.close()
