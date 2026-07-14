"""Tests for the SolarEdge CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from modbus_connection import (
    ModbusConnectionError,
    ModbusExceptionError,
    ModbusTimeoutError,
)
from modbus_connection.decode import decode_float32
from modbus_connection.encode import encode_float32, encode_string
from typer.testing import CliRunner

from solaredged.cli import _grid_display, _on_off, _safe, cli
from solaredged.const import (
    InverterStatus,
    StorageChargePolicy,
    StorageControlMode,
    SunSpecDID,
)
from solaredged.exceptions import SolarEdgeConnectionError, SolarEdgeError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from modbus_connection.mock import MockModbusConnection
    from syrupy.assertion import SnapshotAssertion

runner = CliRunner()


def _words(holding: Mapping[int, Any], *addresses: int) -> list[int]:
    """Read raw register words at the given addresses as a plain ``list[int]``.

    The mock's holding store has a loose value type; this narrows the read back
    to ``list[int]`` for ``decode_float32``.
    """
    return [holding[address] for address in addresses]


@pytest.fixture
def patch_connect(
    monkeypatch: pytest.MonkeyPatch, se17k_connection: MockModbusConnection
) -> Callable[[], MockModbusConnection]:
    """Patch the CLI's ``connect_tcp`` to return the seeded mock connection."""

    async def _fake_connect(_host: str, *, port: int = 1502) -> MockModbusConnection:
        assert port
        return se17k_connection

    monkeypatch.setattr("solaredged.cli.connect_tcp", _fake_connect)
    return lambda: se17k_connection


class _PrunedUnit:
    """Wrap a mock unit, answering an illegal-data-address for chosen bases.

    Reading exactly a pruned base register raises, mirroring a device without
    that optional block. The mock backend otherwise returns zeros for anything
    unseeded, which would make every block look present.
    """

    def __init__(self, inner: Any, absent: set[int]) -> None:
        self._inner = inner
        self._absent = absent

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        if address in self._absent:
            raise ModbusExceptionError(2)
        return await self._inner.read_holding_registers(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _PrunedConnection:
    """A connection whose units treat the given base addresses as absent."""

    def __init__(
        self, *, absent: set[int], inner: MockModbusConnection | None = None
    ) -> None:
        self._inner = inner
        self._absent = absent

    def for_unit(self, unit: int) -> _PrunedUnit:
        inner = self._inner.for_unit(unit) if self._inner is not None else _Empty()
        return _PrunedUnit(inner, self._absent)

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


class _Empty:
    """A unit that answers every read with an illegal-data-address."""

    async def read_holding_registers(self, _address: int, _count: int) -> list[int]:
        raise ModbusExceptionError(2)


def _connect_returning(conn: object) -> Callable[..., object]:
    """Build a ``connect_tcp`` stand-in that returns a fixed connection."""

    async def _connect(_host: str, *, port: int = 1502) -> object:
        assert port
        return conn

    return _connect


@pytest.mark.usefixtures("patch_connect")
def test_info(snapshot: SnapshotAssertion) -> None:
    """The info command renders the inverter status."""
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0
    assert result.output == snapshot


@pytest.mark.usefixtures("patch_connect")
def test_info_json() -> None:
    """The info command emits machine-readable JSON."""
    result = runner.invoke(cli, ["info", "--host", "inverter.local", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["common"]["model"] == "SE17K-RW0T0BNN4"
    assert payload["inverter"]["status"] == int(InverterStatus.PRODUCING)
    assert payload["meters"] == []


@pytest.mark.usefixtures("patch_connect")
def test_dump() -> None:
    """The dump command emits the raw register map."""
    result = runner.invoke(cli, ["dump", "--host", "inverter.local"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["holding"]["40000"] == 21365


def test_info_renders_meter(patch_connect: Callable[[], MockModbusConnection]) -> None:
    """The info output includes a meter panel when a meter is present."""
    conn = patch_connect()
    conn.for_unit(1).holding[40188] = int(SunSpecDID.THREE_PHASE_WYE_METER)
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0
    assert "Meter 1" in result.output


def test_info_renders_battery_and_strings(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The info output includes battery and DC-string panels when present."""
    holding = patch_connect().for_unit(1).holding
    for i, word in enumerate(encode_float32(10000.0, word_order="little")):
        holding[57666 + i] = word  # battery rated energy -> battery detected
    holding[40121] = 160  # MMPPT DID
    holding[40129] = 2  # two DC modules
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0
    assert "Battery 1" in result.output
    assert "DC strings" in result.output


def test_power_limit_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The power-limit command writes the active power limit register."""
    conn = patch_connect()
    result = runner.invoke(
        cli, ["power-limit", "80", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 0
    assert conn.for_unit(1).holding[61441] == 80


def test_storage_mode_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The storage-mode command writes the storage control mode register."""
    conn = patch_connect()
    result = runner.invoke(
        cli,
        [
            "storage-mode",
            "MAXIMIZE_SELF_CONSUMPTION",
            "--host",
            "inverter.local",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    assert conn.for_unit(1).holding[57348] == int(
        StorageControlMode.MAXIMIZE_SELF_CONSUMPTION
    )


def test_cos_phi_command(patch_connect: Callable[[], MockModbusConnection]) -> None:
    """The cos-phi command writes the power factor register."""
    conn = patch_connect()
    result = runner.invoke(cli, ["cos-phi", "0.9", "--host", "inverter.local", "--yes"])
    assert result.exit_code == 0
    holding = conn.for_unit(1).holding
    words = _words(holding, 61442, 61443)
    assert decode_float32(words, word_order="little") == pytest.approx(0.9)


def test_backup_reserve_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The backup-reserve command writes the reserve register (float)."""
    conn = patch_connect()
    result = runner.invoke(
        cli, ["backup-reserve", "20", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 0
    holding = conn.for_unit(1).holding
    words = _words(holding, 57352, 57353)
    assert decode_float32(words, word_order="little") == pytest.approx(20.0)


def test_charge_policy_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The charge-policy command writes the AC charge policy register."""
    conn = patch_connect()
    result = runner.invoke(
        cli, ["charge-policy", "ALWAYS", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 0
    assert conn.for_unit(1).holding[57349] == int(StorageChargePolicy.ALWAYS)


def test_remote_charge_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The remote-charge command sets remote mode, power and timeout."""
    conn = patch_connect()
    result = runner.invoke(
        cli,
        [
            "remote-charge",
            "3000",
            "--host",
            "inverter.local",
            "--timeout",
            "1800",
            "--yes",
        ],
    )
    assert result.exit_code == 0
    holding = conn.for_unit(1).holding
    assert holding[57348] == int(StorageControlMode.REMOTE_CONTROL)
    charge = _words(holding, 57358, 57359)
    assert decode_float32(charge, word_order="little") == pytest.approx(3000.0)
    assert holding[57355] == 1800  # command timeout


def test_charge_limit_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The charge-limit command writes the remote charge-power register."""
    holding = patch_connect().for_unit(1).holding
    result = runner.invoke(cli, ["charge-limit", "2500", "--host", "x", "--yes"])
    assert result.exit_code == 0
    assert decode_float32(
        _words(holding, 57358, 57359), word_order="little"
    ) == pytest.approx(2500.0)


def test_discharge_limit_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The discharge-limit command writes the remote discharge-power register."""
    holding = patch_connect().for_unit(1).holding
    result = runner.invoke(cli, ["discharge-limit", "3500", "--host", "x", "--yes"])
    assert result.exit_code == 0
    assert decode_float32(
        _words(holding, 57360, 57361), word_order="little"
    ) == pytest.approx(3500.0)


def test_remote_discharge_command(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """The remote-discharge command sets remote mode and discharge power."""
    holding = patch_connect().for_unit(1).holding
    result = runner.invoke(cli, ["remote-discharge", "4000", "--host", "x", "--yes"])
    assert result.exit_code == 0
    assert holding[57348] == int(StorageControlMode.REMOTE_CONTROL)
    assert decode_float32(
        _words(holding, 57360, 57361), word_order="little"
    ) == pytest.approx(4000.0)


def test_info_connection_error(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """A dropped connection makes the command exit non-zero."""
    patch_connect().simulate_connection_lost()
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code != 0


def test_connect_failure_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed connect (before probing) reaches the friendly error handler."""

    async def _boom(_host: str, *, port: int = 1502) -> object:
        assert port
        msg = "no route to host"
        raise ModbusConnectionError(msg)

    monkeypatch.setattr("solaredged.cli.connect_tcp", _boom)
    result = runner.invoke(cli, ["info", "--host", "nope.local"])
    assert result.exit_code == 1
    # The raw backend error is wrapped, so the registered handler catches it in
    # real use (CliRunner bypasses the app __call__ that dispatches handlers).
    assert isinstance(result.exception, SolarEdgeConnectionError)


def test_dump_empty_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that answers no block leaves dump with a non-zero exit."""

    async def _connect(_host: str, *, port: int = 1502) -> _PrunedConnection:
        assert port
        return _PrunedConnection(absent={40000, 40069, 57344, 61440})

    monkeypatch.setattr("solaredged.cli.connect_tcp", _connect)
    result = runner.invoke(cli, ["dump", "--host", "inverter.local"])
    assert result.exit_code == 1


@pytest.mark.usefixtures("patch_connect")
def test_write_confirmation_aborts() -> None:
    """Declining the confirmation prompt writes nothing and exits non-zero."""
    result = runner.invoke(
        cli, ["power-limit", "80", "--host", "inverter.local"], input="n\n"
    )
    assert result.exit_code != 0


def test_write_confirmation_accepts(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """Accepting the confirmation prompt performs the write."""
    conn = patch_connect()
    result = runner.invoke(
        cli, ["power-limit", "80", "--host", "inverter.local"], input="y\n"
    )
    assert result.exit_code == 0
    assert conn.for_unit(1).holding[61441] == 80


@pytest.mark.usefixtures("patch_connect")
def test_storage_mode_unknown_rejected() -> None:
    """An unknown storage mode name exits non-zero with the valid choices."""
    result = runner.invoke(
        cli, ["storage-mode", "BOGUS", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 1
    assert "Unknown mode" in result.output


@pytest.mark.usefixtures("patch_connect")
def test_charge_policy_unknown_rejected() -> None:
    """An unknown charge policy name exits non-zero with the valid choices."""
    result = runner.invoke(
        cli, ["charge-policy", "BOGUS", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 1
    assert "Unknown policy" in result.output


def test_power_limit_without_control_block(
    se17k_connection: MockModbusConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing power limit on an inverter without power control exits non-zero."""
    conn = _PrunedConnection(inner=se17k_connection, absent={61440})
    monkeypatch.setattr("solaredged.cli.connect_tcp", _connect_returning(conn))
    result = runner.invoke(
        cli, ["power-limit", "50", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 1
    assert "no power control" in result.output.lower()


def test_storage_command_without_control_block(
    se17k_connection: MockModbusConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup-reserve on an inverter without storage control exits non-zero."""
    conn = _PrunedConnection(inner=se17k_connection, absent={57348})
    monkeypatch.setattr("solaredged.cli.connect_tcp", _connect_returning(conn))
    result = runner.invoke(
        cli, ["backup-reserve", "20", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 1
    assert "no storage" in result.output.lower()


def test_info_renders_unknown_status(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """An unknown inverter status renders as 'Unknown' rather than crashing."""
    patch_connect().for_unit(1).holding[40107] = 99  # not a known I_Status
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0
    assert "Unknown" in result.output


def test_display_helpers_render_none() -> None:
    """The status display helpers render an unknown (None) value as a dash."""
    assert "—" in _grid_display(None)
    assert "—" in _on_off(None)


def test_safe_neutralises_untrusted_device_strings() -> None:
    """Device strings are stripped of control chars and have markup escaped."""
    dirty = "Model[red]X[/red]\x1b[2Jpwn\x07"
    clean = _safe(dirty)
    assert "\x1b" not in clean  # ANSI escape stripped
    assert "\x07" not in clean  # bell stripped
    assert "\\[red]" in clean  # Rich markup escaped, not interpreted


def test_info_escapes_malicious_device_identity(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """A hostile model string cannot inject escape sequences into info output."""
    holding = patch_connect().for_unit(1).holding
    payload = "Evil[red]X[/red]\x1b[2J"
    for i, word in enumerate(encode_string(payload, length=16)):
        holding[40020 + i] = word  # C_Model
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0
    assert "\x1b" not in result.output  # no raw escape reached the terminal
    # The markup was escaped, not interpreted: the brackets survive literally.
    assert "[red]" in result.output


def test_info_stripped_device_has_no_controls(
    se17k_connection: MockModbusConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inverter with no control blocks renders without a Controls panel."""
    conn = _PrunedConnection(
        inner=se17k_connection,
        absent={40121, 40188, 57666, 57348, 57344, 61440, 61696},
    )
    monkeypatch.setattr("solaredged.cli.connect_tcp", _connect_returning(conn))
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0
    assert "Controls" not in result.output


def test_info_unimplemented_inverter_fields(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """Unimplemented grid status and heatsink temperature render gracefully."""
    holding = patch_connect().for_unit(1).holding
    holding[40113] = 0xFFFF  # grid status word-swapped nan -> on_grid None
    holding[40114] = 0xFFFF
    holding[40103] = 0x8000  # heatsink int16 nan -> None (row is skipped)
    result = runner.invoke(cli, ["info", "--host", "inverter.local"])
    assert result.exit_code == 0


def test_dump_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed connect during dump surfaces as our connection error."""

    async def _boom(_host: str, *, port: int = 1502) -> object:
        assert port
        msg = "unreachable"
        raise ModbusConnectionError(msg)

    monkeypatch.setattr("solaredged.cli.connect_tcp", _boom)
    result = runner.invoke(cli, ["dump", "--host", "inverter.local"])
    assert result.exit_code == 1


def test_dump_transport_failure(
    patch_connect: Callable[[], MockModbusConnection],
) -> None:
    """A transport failure mid-dump (not a missing block) exits non-zero."""
    patch_connect().simulate_connection_lost()
    result = runner.invoke(cli, ["dump", "--host", "inverter.local"])
    assert result.exit_code != 0


def test_storage_mode_without_control_block(
    se17k_connection: MockModbusConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """storage-mode on an inverter without storage control exits non-zero."""
    conn = _PrunedConnection(inner=se17k_connection, absent={57348})
    monkeypatch.setattr("solaredged.cli.connect_tcp", _connect_returning(conn))
    result = runner.invoke(
        cli, ["storage-mode", "BACKUP_ONLY", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code == 1
    assert "no storage control" in result.output.lower()


def test_cos_phi_without_control_block(
    se17k_connection: MockModbusConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cos-phi on an inverter without power control exits non-zero."""
    conn = _PrunedConnection(inner=se17k_connection, absent={61440})
    monkeypatch.setattr("solaredged.cli.connect_tcp", _connect_returning(conn))
    result = runner.invoke(cli, ["cos-phi", "0.9", "--host", "inverter.local", "--yes"])
    assert result.exit_code == 1
    assert "no power control" in result.output.lower()


@pytest.mark.parametrize("fail_register", [57357, 57358, 57355])
def test_remote_charge_partial_write_stays_out_of_remote_control(
    patch_connect: Callable[[], MockModbusConnection], fail_register: int
) -> None:
    """If any staged write fails, control_mode is never flipped to REMOTE_CONTROL.

    command_mode (57357), charge_limit (57358) and command_timeout (57355) are
    staged first; control_mode (57348) is written last, so a mid-sequence failure
    leaves the inverter out of remote control and prints how to recover.
    """
    conn = patch_connect()
    unit = conn.for_unit(1)
    unit.holding[57348] = int(StorageControlMode.MAXIMIZE_SELF_CONSUMPTION)
    unit.fail_write(fail_register, ModbusTimeoutError("boom"))
    result = runner.invoke(
        cli, ["remote-charge", "3000", "--host", "inverter.local", "--yes"]
    )
    assert result.exit_code != 0
    assert "restore" in result.output.lower()
    # control_mode was never advanced to REMOTE_CONTROL.
    assert unit.holding[57348] == int(StorageControlMode.MAXIMIZE_SELF_CONSUMPTION)


def test_connection_error_handler() -> None:
    """The connection error handler exits with a panel."""
    with pytest.raises(SystemExit):
        cli.error_handlers[SolarEdgeConnectionError](SolarEdgeConnectionError("boom"))


def test_solaredge_error_handler() -> None:
    """The generic error handler exits."""
    with pytest.raises(SystemExit):
        cli.error_handlers[SolarEdgeError](SolarEdgeError("boom"))
