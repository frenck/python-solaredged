# SunSpec model definitions (vendored)

`model_1.json` (common), `model_103.json` (three-phase inverter) and
`model_203.json` (three-phase wye meter) are copied from the SunSpec Alliance
model repository (only the JSON whitespace is normalised; the content is
identical):

https://github.com/sunspec/models (json/), licensed Apache-2.0.

They are the authoritative point layout used by `test_sunspec_conformance.py` to
check the register map (addresses, sign, size, scale factors) against the spec.
