# Third-party licenses

python-solaredged is licensed MIT. It bundles no third-party source code in its
own wheel; the notes below cover the dependencies it installs and the reference
data vendored for its test suite.

## Runtime dependency

The sole runtime dependency is [`modbus-connection`][modbus-connection]
(Apache-2.0), which owns the transport and the SunSpec field decoding. It is
installed as a normal package dependency and ships under its own license, not
bundled into this package's source.

The optional extras pull in additional third-party packages, only when you opt
into them, each under its own license:

- `solaredged[cli]`: rich, textual, textual-plotext, typer.
- `solaredged[pymodbus]`: the pymodbus backend for modbus-connection.
- `solaredged[tmodbus]`: the tmodbus backend for modbus-connection.

The release workflow generates a CycloneDX SBOM and attaches it to each build,
so the dependency set is verifiable from the published artifact.

## Vendored reference data (test only)

`tests/fixtures/sunspec/` contains SunSpec model definitions (`model_1`,
`model_103`, `model_203`) copied from the [`sunspec/models`][sunspec-models]
project (Apache-2.0). They are the authoritative point layout used to check the
register map against the spec. They are used only in the test suite and are not
distributed in the wheel. See `tests/fixtures/sunspec/README.md` for details.

The community fixtures under `tests/fixtures/community/` are raw register values
reconstructed from Home Assistant diagnostics dumps. They are factual device
readings, not third-party source code, and are re-expressed in this project's
own format.

[modbus-connection]: https://github.com/home-assistant-libs/modbus-connection
[sunspec-models]: https://github.com/sunspec/models
