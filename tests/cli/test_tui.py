"""Smoke tests for the SolarEdge live TUI.

The TUI is excluded from coverage (it is thin glue over textual), but these run
it headless via textual's test harness to catch structural regressions: that it
mounts, polls a device, runs a write worker, and tears down on quit without the
poll worker cancelling the write or the connection closing underneath it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from solaredged.cli import tui

if TYPE_CHECKING:
    from modbus_connection.mock import MockModbusConnection


@pytest.fixture
def tui_connection(
    monkeypatch: pytest.MonkeyPatch, se17k_connection: MockModbusConnection
) -> MockModbusConnection:
    """Patch the TUI's ``connect_tcp`` to hand back a seeded mock connection."""

    async def _connect(_host: str, *, port: int = 1502) -> MockModbusConnection:
        assert port
        return se17k_connection

    monkeypatch.setattr(tui, "connect_tcp", _connect)
    return se17k_connection


@pytest.mark.usefixtures("tui_connection")
async def test_tui_mounts_polls_and_quits() -> None:
    """The app mounts, polls the device, and quits cleanly (on_unmount runs)."""
    app = tui.SolarEdgeTuiApp(host="x", port=1502, unit=1)
    async with app.run_test() as pilot:
        await pilot.pause()  # on_mount + first poll
        assert app._client is not None  # probed and updated
        await pilot.press("q")  # quit -> on_unmount closes the connection


async def test_tui_write_worker_survives_quit(
    tui_connection: MockModbusConnection,
) -> None:
    """A write kicked off just before quit lands on the register, not cancelled."""
    app = tui.SolarEdgeTuiApp(host="x", port=1502, unit=1)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._set_power_limit(55)
        await pilot.press("q")  # on_unmount waits for the write before closing

    assert tui_connection.for_unit(1).holding[61441] == 55


@pytest.mark.usefixtures("tui_connection")
async def test_tui_power_limit_dialog_opens() -> None:
    """The power-limit key binding opens the dialog on a control-capable device."""
    app = tui.SolarEdgeTuiApp(host="x", port=1502, unit=1)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")  # power-limit binding
        await pilot.pause()
        assert isinstance(app.screen, tui.PowerLimitDialog)
        await pilot.press("escape")  # cancel, leaves the register untouched
