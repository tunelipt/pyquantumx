# pyquantumx

Python access to HBM QuantumX modules through HBM's legacy QuantumX .NET API
using pythonnet. By default it loads the 2009 API (HBM.QuantumX.dll 1.5.x, which matches
the module firmware) from a `QuantumX API\DLLs` folder next to this repository:

```
some-folder\
  QuantumX API\DLLs\   HBM.QuantumX.dll, HBM.DeviceComponents.dll, HBM_QX_Framework.dll
  pyquantumx\          this repository
```

If the DLLs live elsewhere, set `QX_API_DIR` or pass `QuantumX(api_dir=...)`.

## One-time setup

The native `HBM_QX_Framework.dll` is **32-bit only**, so Python must be the 32-bit build.

1. Install **Python 3.13 (or 3.12), Windows installer (32-bit)** from python.org.
   pythonnet 3.0.5 does not support Python 3.14 yet.
2. In this folder:

   ```bat
   py -3.13-32 -m venv .venv
   .venv\Scripts\activate
   pip install -e .
   ```
3. .NET Framework 4.x must be present (it is on every Windows 10/11 machine).

## Check that it works

```bat
python check_env.py
```

It prints the Python bitness, the HBM framework DLL version, your network
adapters and any QuantumX modules found on the network.

## Examples

Connector numbers are **0-based** everywhere, in the scripts and in the library, matching
the .NET API: socket 1 on the module is connector 0, socket 8 of an MX840 is connector 7.

```bat
python examples\scan_and_read.py                  :: config summary + live values
python examples\record.py --connectors 0 1 2 --rate 1200 --seconds 10 --out run1.csv
```

### Several modules, synchronized (`examples\sync_record.py`)

Records from 1, 2 or more modules at once into a single CSV. All modules start
together, and the rows are lined up using the modules' own timestamps. Every
channel uses the same sample rate (`--rate`, default 1200 Hz), which is set on
the modules.

Different connectors on each module — one `--channels MODULE:CONNECTORS` entry per module
(MODULE = IP, name or UUID hex; connectors 0-based, comma-separated, ranges allowed):

```bat
python examples\sync_record.py --channels 192.168.0.217:0 192.168.0.218:0-2 --seconds 10 --out run.csv
python examples\sync_record.py --channels 192.168.0.217:0,3,4 192.168.0.218:1,6 --rate 2400
```

Same connectors on every module (all modules found if `--modules` is omitted):

```bat
python examples\sync_record.py --modules 192.168.0.217 192.168.0.218 --connectors 0 1 2
```

Columns are named `<module>_c<connector>`, e.g. `MX840@192.168.0.217_c0`. The script
prints each module's first timestamp. A spread of 0 means the start was synchronized.
For a synchronized start the modules must also be linked by the FireWire (IEEE 1394)
cable. Without it, the modules' clocks run independently and the script prints a warning.
To compare, add `--no-sync` for a plain, unsynchronized start.

## Library use

```python
from pyquantumx import QuantumX

with QuantumX() as qx:
    dev = qx.connect(qx.scan()[0])          # or qx.connect("192.168.1.20")
    print(qx.describe(dev.uuid))
    print(qx.read_single(dev.uuid, connector=0))   # connectors are 0-based
    with qx.acquisition(dev.uuid, connectors=[0, 1]) as acq:
        chunks = acq.poll()                  # {connector: Chunk(t, values, states)}
        print(chunks[0].values, chunks[0].unix_time())
```

`qx.acquisition()` covers a single module. `QXSimpleDAQ` is one shared acquisition for
all modules, so for several modules subscribe every signal yourself with a unique
UserSignalID and call `StartDAQ(True)` once (see `examples\sync_record.py`).

The raw .NET objects are always reachable: `qx.S` (QXSystem), `qx.DAQ`
(QXSimpleDAQ), `qx.read_device(uuid)` (Device object model), and
`pyquantumx.net_type("eSensorTypes")` for enums/classes.

## Notes / limitations

* Tested on two MX840 modules with the 2009 API (Nov 2026):
  single values, single-module recording (`record.py`, 1200/2400 Hz) and synchronized
  two-module recording (`sync_record.py`, identical start timestamps over the FireWire link).
  Other module types are untested.
* Sample rates: `pyquantumx.sample_rates()` lists every output rate the API knows
  (0.1 Hz ... 96 kHz, from the internal `eOutputRates` enum). It is one list for all module
  types; an MX840 goes up to 19200 Hz. `set_sample_rate` rounds a requested rate to the
  nearest listed one, and the module itself aligns rates it doesn't support (both with a warning).
* Samples carry module timestamps: seconds since 2000-01-01 (`Chunk.t`), `Chunk.unix_time()` for Unix time.
* With several network cards, pass the adapter index: `qx.scan(1)` or `record.py --adapter 1`.
* API from 2009: newer QuantumX modules or firmware may not be supported.
* Only one process can own the modules at a time; close catman/MX Assistant first.
