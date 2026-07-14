"""Run the bundled examples against a mock device so they cannot silently rot.

The examples in ``examples/`` are user-facing: if the public API changes under
them, they should fail here rather than in someone's terminal. Each is loaded,
its ``connect_tcp`` is redirected at a seeded mock connection, and its ``main``
coroutine is run end to end.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from modbus_connection.mock import MockModbusConnection

from .conftest import load_registers

if TYPE_CHECKING:
    from types import ModuleType

_EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _load_example(name: str) -> ModuleType:
    """Import an example module by file name, without an examples package."""
    spec = importlib.util.spec_from_file_location(
        f"example_{name}", _EXAMPLES_DIR / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["read", "control"])
async def test_example_runs(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each example probes, acts on, and reports a seeded device without error."""
    connection = MockModbusConnection()
    connection.for_unit(1).holding.update(load_registers("se17k_3phase.json"))

    async def _connect(_host: str, *, port: int = 1502) -> MockModbusConnection:
        assert port
        return connection

    module = _load_example(name)
    monkeypatch.setattr(module, "connect_tcp", _connect)

    await module.main()

    assert capsys.readouterr().out  # the example produced output


def test_every_example_is_covered() -> None:
    """No example script is left unrun by this test (guard against new drift)."""
    scripts = {path.stem for path in _EXAMPLES_DIR.glob("*.py")}
    assert scripts == {"read", "control"}
