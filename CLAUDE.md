# CLAUDE.md

Python wrapper (`pyquantumx`) for HBM QuantumX measurement modules, built on HBM's legacy
QuantumX .NET API (2009, HBM.QuantumX.dll 1.5.x) through pythonnet.
GitHub: tunelipt/pyquantumx. MIT license.

## Environment

- **Windows + 32-bit CPython 3.12/3.13 only.** `HBM_QX_Framework.dll` is x86-only, and
  pythonnet 3.0.5 does not support Python 3.14. Use `py -3.13-32` to get the 32-bit interpreter.
- Setup: `py -3.13-32 -m venv .venv`, then `.venv\Scripts\pip install -e .` (deps: pythonnet, numpy<2.6).
- Uses the .NET Framework 4.x runtime (`pythonnet.load("netfx")`).
- The DLLs are **not** in this repo. Default location: `..\QuantumX API\DLLs` (a sibling of
  the checkout). Override with `QX_API_DIR` or `QuantumX(api_dir=...)`.
- The API docs are `..\QuantumX API\Documentation\QXAPIDocu.chm`; an extracted HTML copy is
  `..\QXAPIDocu.zip`. HBM's C# demo is `..\QuantumX API\Examples\DotNet\Form1.cs`.
  **Look up API behaviour in the docs/demo before guessing.** Unzip into a scratch folder
  and grep the HTML (pages are named like `HBM_QuantumX_QXSystem_AssignSensor@UInt64@int@Sensor.html`).
- Without pythonnet/numpy installed (or without the modules on the network), only syntax
  checks are possible: `python -m py_compile pyquantumx/*.py examples/*.py check_env.py`.
  Say clearly what was and wasn't tested on hardware.

## Layout

- `pyquantumx/quantumx.py`: the whole library (`QuantumX`, `Acquisition`, `Chunk`,
  `ScanInfo`, `ModuleInfo`, `QuantumXError`, `load_api`, `net_type`, rate/filter lists).
- `pyquantumx/__init__.py`: public exports. Add new public functions to both the import and `__all__`.
- `examples/`: `scan_and_read.py` (config summary + live values, writes `device_config.xml`),
  `record.py` (one module to CSV), `sync_record.py` (several modules, synchronized, one CSV).
- `check_env.py`: checks bitness, DLLs, adapters, scan.
- `pyproject.toml`, `requirements.txt`, `README.md` (user docs; keep in sync with the code).

## API model (from the docs)

```
Device -> Connectors[i] -> AnalogInChannel -> Sensor (SensorType + ScalingType)
                                           -> ZeroOffset
                                           -> SyncSignals[0..2]  (rate, low-pass filter, unit, name)
                        -> CanBus -> CanInChannels[j] (AsyncSignal, event-driven)
```

- **Connector** = physical socket. **Channel** = always 0 for analog inputs; only a CAN bus
  connector has several channels. **Signal** = one output stream of a channel (`SyncSignal.Number`).
- `QXSystem` (`qx.S`): configuration (Read*/Assign*). `QXSimpleDAQ` (`qx.DAQ`): one global
  acquisition for all modules: `SubscribeSignal(uuid, connector, channel, signal, type, userID, bufBytes)`,
  `StartDAQ(True)` for a synchronized start (needs the FireWire link), `GetDataBlock`.
- Several enums are **internal** (`eOutputRates`, `eFilterFrequencies`, `eExcitationVoltages`).
  Read them by reflection through `_internal_enum_hz()`. Their values are in mHz.
- These lists are global for all module types; a module accepts only some values and the
  API has no per-module query.
- `GetSinglePoint`: use the (uuid, connector, signal, eScaleType, out double) overload; the
  demo's other overload returns "Wrong signal index" on an MX840.

## Conventions

- **Connector numbers are 0-based everywhere**: library, example CLIs, README, CSV
  column names (`connector_0`, `<module>_c0`). Socket 1 on the module = connector 0.
  Exception: the API's own warning texts and the device XML (`<Connector1>`) are 1-based;
  convert when parsing them.
- Wrap .NET calls with `self._call(fn, *args)` (raises `QuantumXError` with
  `ReadLastProblems()` details).
- Use `self._assign(fn, *args)` for Assign* calls. AssignFailed with warnings only means the
  module adjusted a value: that becomes a Python warning, not an error.
- Read-modify-write: start from the module's current object (`ReadSyncSignal`,
  `read_device`) rather than a fresh one, so unrelated settings are kept. Return what the
  module actually stored (read back after assigning).
- Values that must come from a fixed list (rates, cut-offs) are rounded to the nearest
  allowed value on a log scale, with a warning (`nearest_sample_rate`, `nearest_filter_frequency`).
- Prefer the narrowest Assign* call: e.g. `AssignConnectorType`, not `AssignConnector`,
  which replaces the whole connector.
- Keep the raw .NET objects reachable (`qx.S`, `qx.DAQ`, `qx.read_device`, `qx.api.dc`, `net_type`).

## Hardware facts (tested Nov 2026)

- Two MX840 modules on 192.168.0.217 and 192.168.0.218, linked by FireWire. A synchronized
  two-module start gave identical first timestamps.
- MX840: **one SyncSignal per connector**, output rates up to **19200 Hz** (catman confirms).
  Default seen: voltage sensor, 10.2 V range, Bessel low-pass 2000 Hz, 19200 Hz.
- Timestamps are seconds since 2000-01-01 (`Chunk.unix_time()` converts).
- Only one program can own the modules: close catman/MX Assistant first.
- Not yet verified on hardware: `sample_rates()`, `set_filter`, `set_bridge`/`set_voltage`
  (the unit of `MaxElectrical`, and whether 2.5 V excitation is accepted), and how `describe()`
  prints .NET enums.

## Git

- Work on `main` and push when the user asks (`git push origin main`).
- Generated files are ignored (`*.csv`, `device_config.xml`, `.venv/`). Don't `git add -A`
  without checking `git status` for files the examples wrote.
