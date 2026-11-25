"""
Python wrapper for HBM's legacy QuantumX .NET API (HBM.QuantumX.dll v1.5.x) via pythonnet.

Requirements
------------
* Windows, **32-bit** CPython (HBM_QX_Framework.dll is x86-only), Python 3.7-3.13
* pip install "pythonnet>=3.0.5" "numpy<2.6"
* The folder with HBM.QuantumX.dll, HBM.DeviceComponents.dll and HBM_QX_Framework.dll.
  By default ..\\..\\QuantumX API\\DLLs relative to this file; override with the
  QX_API_DIR environment variable or the api_dir argument.

Calls mirror the C# demo shipped with the API (Examples\\Cs\\QXAPIDemo\\Form1.cs).
"""

from __future__ import annotations

import atexit
import ctypes
import math
import os
import re
import struct
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence, Union

import numpy as np

# 2009 API (HBM.QuantumX.dll 1.5.x) that matches the module firmware, in a
# "QuantumX API" folder next to the repository checkout.
DEFAULT_API_DIR = Path(__file__).resolve().parents[2] / "QuantumX API" / "DLLs"
REQUIRED_FILES = ("HBM.QuantumX.dll", "HBM.DeviceComponents.dll", "HBM_QX_Framework.dll")

_api: Optional[SimpleNamespace] = None


class QuantumXError(RuntimeError):
    """Raised for setup problems and wraps .NET exceptions with the API's problem list."""


# --------------------------------------------------------------------------------------
# Loading the .NET assemblies
# --------------------------------------------------------------------------------------
def load_api(api_dir: Union[str, Path, None] = None) -> SimpleNamespace:
    """Load the HBM assemblies once and return a namespace with the .NET types."""
    global _api
    if _api is not None:
        return _api

    if sys.platform != "win32":
        raise QuantumXError("The QuantumX API only runs on Windows.")
    bits = struct.calcsize("P") * 8
    if bits != 32:
        raise QuantumXError(
            f"This Python is {bits}-bit, but HBM_QX_Framework.dll is 32-bit only. "
            "Install the 32-bit (x86) Windows build of Python 3.13 or older and run from it."
        )

    api_dir = Path(api_dir or os.environ.get("QX_API_DIR") or DEFAULT_API_DIR).resolve()
    missing = [f for f in REQUIRED_FILES if not (api_dir / f).is_file()]
    if missing:
        raise QuantumXError(
            f"Missing in {api_dir}: {', '.join(missing)}. "
            "Set QX_API_DIR or pass api_dir= to point at the QuantumX API DLLs folder."
        )

    # Make the native HBM_QX_Framework.dll findable for the .NET P/Invoke calls.
    os.environ["PATH"] = str(api_dir) + os.pathsep + os.environ.get("PATH", "")
    os.add_dll_directory(str(api_dir))
    ctypes.windll.kernel32.SetDllDirectoryW(str(api_dir))

    # The assemblies are .NET Framework (CLR 2.0) -> use the .NET Framework 4.x runtime.
    from pythonnet import load as _load_runtime

    try:
        _load_runtime("netfx")
    except RuntimeError:
        pass  # runtime already loaded (e.g. clr imported earlier)

    import clr  # noqa: E402  (available after pythonnet.load)

    clr.AddReference(str(api_dir / "HBM.DeviceComponents.dll"))
    clr.AddReference(str(api_dir / "HBM.QuantumX.dll"))

    import HBM.DeviceComponents as dc  # type: ignore
    import HBM.QuantumX as qx  # type: ignore

    _api = SimpleNamespace(dir=api_dir, qx=qx, dc=dc, QXSystem=qx.QXSystem, QXSimpleDAQ=qx.QXSimpleDAQ)
    return _api


def net_type(name: str):
    """Look up a .NET type/enum (e.g. 'eConnectorTypes', 'Sensor') in either HBM namespace."""
    api = load_api()
    for ns in (api.dc, api.qx):
        if hasattr(ns, name):
            return getattr(ns, name)
    raise AttributeError(f"{name} not found in HBM.DeviceComponents or HBM.QuantumX")


_sample_rates: Optional[List[float]] = None


def sample_rates() -> List[float]:
    """All output rates (Hz) the API knows, ascending, from HBM.DeviceComponents.eOutputRates.

    The enum is internal, so it is read by reflection; its values are in mHz. This is one list
    for every module type: a given module supports only some of them (an MX840 tops out at
    19200 Hz), and the API has no per-module query.
    """
    global _sample_rates
    if _sample_rates is None:
        import clr  # noqa: E402
        import System  # type: ignore  # noqa: E402

        asm = clr.GetClrType(load_api().dc.SyncSignal).Assembly
        t = asm.GetType("HBM.DeviceComponents.eOutputRates")
        if t is None:
            raise QuantumXError("HBM.DeviceComponents.eOutputRates not found in the API DLL")
        _sample_rates = sorted(System.Convert.ToInt64(v) / 1000.0 for v in System.Enum.GetValues(t))
    return list(_sample_rates)


def nearest_sample_rate(rate_hz: float) -> float:
    """The rate in sample_rates() closest to rate_hz (compared on a log scale)."""
    if rate_hz <= 0:
        raise ValueError(f"Sample rate must be positive, got {rate_hz}")
    return min(sample_rates(), key=lambda r: abs(math.log(r / rate_hz)))


def _outs(result, n: int) -> tuple:
    """Normalise pythonnet's out-parameter return (tuple or bare value) to the last n outs."""
    if isinstance(result, tuple):
        return result[-n:]
    if n == 1:
        return (result,)
    raise QuantumXError(f"Unexpected out-parameter result: {result!r}")


def _array(seq, n: int, dtype, fill) -> np.ndarray:
    """First n items of a .NET array as numpy, or a filled array if the field is missing/short."""
    if seq is None:
        return np.full(n, fill, dtype=dtype)
    try:
        return np.fromiter(seq, dtype=dtype, count=n)
    except Exception:
        return np.full(n, fill, dtype=dtype)


def _uuid_hex(uuid) -> str:
    try:
        return f"{int(uuid):012X}"
    except Exception:
        return str(uuid)


# --------------------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------------------
@dataclass
class ScanInfo:
    name: str
    ip: str
    port: int
    uuid: object  # .NET UUID value, pass back to API calls unchanged
    raw: object = field(repr=False, default=None)

    @property
    def uuid_hex(self) -> str:
        return _uuid_hex(self.uuid)

    def __str__(self) -> str:
        return f"{self.name}  {self.ip}:{self.port}  UUID={self.uuid_hex}"


@dataclass
class ModuleInfo:
    uuid: object
    device_type: str
    parent: Optional[object] = None

    @property
    def uuid_hex(self) -> str:
        return _uuid_hex(self.uuid)


# --------------------------------------------------------------------------------------
# Main wrapper
# --------------------------------------------------------------------------------------
class QuantumX:
    """High-level access to one or more QuantumX modules.

    Use as a context manager so the API is disconnected and unloaded on exit:

        with QuantumX() as qx:
            dev = qx.connect(qx.scan()[0])
            print(qx.read_single(dev.uuid, connector=0))
    """

    def __init__(self, api_dir: Union[str, Path, None] = None, trace_log: bool = False):
        self.api = load_api(api_dir)
        self.S = self.api.QXSystem
        self.DAQ = self.api.QXSimpleDAQ
        self._connected_ips: List[str] = []
        self._last_scan: List[ScanInfo] = []
        if trace_log:
            self.S.UseTraceLog = True
        atexit.register(self.close)

    # -- context manager ---------------------------------------------------------------
    def __enter__(self) -> "QuantumX":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- info / discovery ---------------------------------------------------------------
    def dll_version(self) -> str:
        return str(self.S.ReadDllVersion())

    def network_adapters(self) -> List[Dict[str, str]]:
        out = []
        for a in self.S.ReadNetworkAdapters():
            out.append(
                dict(
                    name=str(a.AdapterName),
                    description=str(a.Description),
                    ip=str(a.IPAddress),
                    mac=str(a.PhysicalAddress),
                    type=str(a.AdapterType),
                )
            )
        return out

    def scan(self, adapters: Union[int, Sequence[int], None] = None) -> List[ScanInfo]:
        """Scan for QuantumX modules (sorted by name).

        adapters: None = API default (first available adapter), an adapter index, or a list
        of indices (see network_adapters()). Use this if the PC has several network cards.
        """
        try:
            if adapters is None:
                results = list(self.S.ScanForQXDevices())
            else:
                results = []
                for i in ([adapters] if isinstance(adapters, int) else adapters):
                    results.extend(self.S.ScanForQXDevices(int(i)))
        except Exception as ex:
            raise QuantumXError(f"Scan failed: {ex}") from ex
        found = [ScanInfo(str(r.Name), str(r.IPAddress), int(r.Port), r.UUID, r) for r in results]
        found.sort(key=lambda s: s.name)
        self._last_scan = found
        return found

    # -- connection ----------------------------------------------------------------------
    def connect(self, target: Union[ScanInfo, str], port: Optional[int] = None) -> ScanInfo:
        """Connect by ScanInfo, IP address or module name. Returns the ScanInfo used."""
        info = self._resolve(target)
        p = port if port is not None else info.port
        try:
            self.S.Connect(info.ip, p)
        except Exception as ex:
            raise QuantumXError(f"Connect to {info} failed: {ex}") from ex
        if info.ip not in self._connected_ips:
            self._connected_ips.append(info.ip)
        return info

    def _resolve(self, target: Union[ScanInfo, str]) -> ScanInfo:
        if isinstance(target, ScanInfo):
            return target
        scan = self._last_scan or self.scan()
        for s in scan:
            if target in (s.ip, s.name, s.uuid_hex):
                return s
        raise QuantumXError(f"No scanned module matches {target!r}. Found: {[str(s) for s in scan]}")

    def disconnect(self, ip: Optional[str] = None) -> None:
        for addr in ([ip] if ip else list(self._connected_ips)):
            try:
                self.S.Disconnect(addr)
            finally:
                if addr in self._connected_ips:
                    self._connected_ips.remove(addr)

    def close(self) -> None:
        """Stop DAQ, disconnect everything and unload the native framework. Safe to call twice."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        for fn in (lambda: self.DAQ.StopDAQ(), self.disconnect, lambda: self.S.Unload()):
            try:
                fn()
            except Exception:
                pass

    def modules(self) -> List[ModuleInfo]:
        """All connected modules incl. modules behind a CX27 (flat list)."""
        out = []
        for dev in self.S.SubDevicesHierarchical:
            out.append(ModuleInfo(dev.UUID, str(dev.DeviceType)))
            for sd in dev.SubDevices:
                out.append(ModuleInfo(sd.UUID, str(sd.DeviceType), parent=dev.UUID))
        return out

    # -- configuration ---------------------------------------------------------------------
    def read_device(self, uuid):
        """Return the .NET Device object (Connectors -> AnalogInChannel -> SyncSignals ...)."""
        return self.S.ReadDevice(uuid)

    def device_xml(self, uuid) -> str:
        return str(self.read_device(uuid).ConvertToXML())

    def describe(self, uuid) -> List[Dict]:
        """Short summary per connector: index, analog-in sync-signal output rates."""
        dev = self.read_device(uuid)
        rows = []
        for i, con in enumerate(dev.Connectors):
            row = {"connector": i, "rates_hz": []}
            ain = getattr(con, "AnalogInChannel", None)
            if ain is not None and getattr(ain, "SyncSignals", None) is not None:
                row["rates_hz"] = [s.OutputRate for s in ain.SyncSignals]
                row["signal_numbers"] = [int(s.Number) for s in ain.SyncSignals]
            rows.append(row)
        return rows

    def set_sample_rate(self, uuid, rate_hz: float, connectors: Optional[Iterable[int]] = None) -> Dict[int, float]:
        """Set the output rate of the analog-in sync signals and return {connector: rate actually set}.

        rate_hz is first rounded to the nearest rate in sample_rates() (e.g. 1000 -> 1200 Hz),
        with a Python warning. A module supports only part of that list; if it rejects the rate
        (e.g. 48000 Hz on an MX840) it aligns to the nearest rate it allows and the API raises
        AssignFailed with warnings only. That case is treated as success: a Python warning is
        issued and the aligned rate is returned. Real errors still raise QuantumXError.
        Signal 2 of a channel (if present) is capped at 19200 Hz.
        """
        requested = rate_hz
        rate_hz = nearest_sample_rate(float(rate_hz))
        if rate_hz != requested:
            warnings.warn(f"{requested} Hz is not an API output rate; using {rate_hz} Hz.", stacklevel=2)
        dev = self.read_device(uuid)
        idx = list(range(len(dev.Connectors)) if connectors is None else connectors)

        def apply(rates: Dict[int, float]) -> List[str]:
            d = self.read_device(uuid)
            for i, r in rates.items():
                ain = d.Connectors[i].AnalogInChannel
                if ain is None:
                    continue
                for j, sig in enumerate(ain.SyncSignals):
                    sig.OutputRate = min(r, 19200.0) if j == 1 else float(r)
            try:
                self.S.AssignConnectors(uuid, d)
                return []
            except Exception as ex:
                probs = list(self.S.ReadLastProblems())
                if probs and all(str(p.Type).lower().startswith("warn") for p in probs):
                    return [str(p.Text) for p in probs]
                details = "; ".join(f"{p.Type}: {p.Text}" for p in probs)
                raise QuantumXError(f"AssignConnectors failed: {ex}" + (f" | {details}" if details else "")) from ex

        warns = apply({i: float(rate_hz) for i in idx})
        actual = self._rates(uuid, idx)

        if warns:
            # Use the aligned rate the module reported, and make sure it was actually stored.
            aligned = {}
            for w in warns:
                m = re.search(r"set to:\s*([0-9.]+).*?connector\s*(\d+)", w)
                if m:
                    aligned[int(m.group(2)) - 1] = float(m.group(1))  # messages use 1-based connectors
            if aligned and any(abs(actual.get(i, -1) - r) > 1e-6 for i, r in aligned.items() if i in actual):
                apply({i: r for i, r in aligned.items() if i in idx})
                actual = self._rates(uuid, idx)
            uniq = sorted(set(actual.values()))
            warnings.warn(f"{rate_hz} Hz is not supported by this module; module set {uniq} Hz.", stacklevel=2)
        return actual

    def _rates(self, uuid, connectors: Iterable[int]) -> Dict[int, float]:
        d = self.read_device(uuid)
        out = {}
        for i in connectors:
            ain = d.Connectors[i].AnalogInChannel
            if ain is not None and len(ain.SyncSignals) > 0 and ain.SyncSignals[0].OutputRate is not None:
                out[i] = float(ain.SyncSignals[0].OutputRate)
        return out

    def zero(self, uuid, connectors: Optional[Iterable[int]] = None, channel: int = 0) -> None:
        n = len(self.read_device(uuid).Connectors)
        for i in (range(n) if connectors is None else connectors):
            self._call(self.S.AssignZero, uuid, i, channel)

    def set_teds_usage(self, uuid, mode: str = "AUTO", connectors: Optional[Iterable[int]] = None) -> None:
        """mode: 'NO', 'AUTO' or 'REQUIRED'."""
        e = getattr(net_type("eTedsUsages"), mode.upper())
        n = len(self.read_device(uuid).Connectors)
        for i in (range(n) if connectors is None else connectors):
            self._call(self.S.AssignTedsUsage, uuid, i, e)

    def rename(self, uuid, name: str) -> None:
        self._call(self.S.AssignModuleName, uuid, name)

    def blink(self, uuid, on: bool = True) -> None:
        """Blink all connector LEDs orange (identify a module), or restore normal LEDs."""
        mode = getattr(net_type("eLedMode"), "Blink_Orange" if on else "Normal")
        for i in range(len(self.read_device(uuid).Connectors)):
            self.S.SetLedMode(uuid, i, mode)

    def factory_defaults(self, uuid) -> None:
        self._call(self.S.AssignDeviceDefaults, uuid)

    def problems(self) -> List[str]:
        """Detailed problems from the last failed Assign* call."""
        try:
            return [f"{p.Type}: {p.Text} ({p.Value})" for p in self.S.ReadLastProblems()]
        except Exception:
            return []

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as ex:
            details = "; ".join(self.problems())
            raise QuantumXError(f"{getattr(fn, '__name__', fn)} failed: {ex}" + (f" | {details}" if details else "")) from ex

    # -- single values --------------------------------------------------------------------
    def read_single(self, uuid, connector: int, signal: int = 0, scale: str = "phys") -> float:
        """One value from an analog-in signal. connector and signal are 0-based.

        Uses GetSinglePoint(UInt64, int Connector, int Signal, eScaleType, out double).
        The other overload (..., Channel, Signal, eConnectorTypes, out float), which HBM's
        C# demo uses, answers "#-1014110 Wrong signal index" for every signal on an MX840
        (tested 2026-11-16), so it is not used.

        scale: 'phys' (physical units, default), 'electrical' or 'none' (unscaled).
        """
        stype = getattr(net_type("eScaleType"), {"phys": "SCALETP_PHYS", "electrical": "SCALETP_ELECTRICAL",
                                                  "none": "SCALETP_NONE"}[scale.lower()])
        res = self._call(self.DAQ.GetSinglePoint, uuid, connector, signal, stype, 0.0)
        (value,) = _outs(res, 1)
        return float(value)

    def read_all(self, uuid):
        """Current value of every signal of a module (QXSimpleDAQ.GetSingleShot).

        Returns (values: np.ndarray, states: list[str]).
        """
        res = self.DAQ.GetSingleShot(uuid, True, None, None)
        vals, states = _outs(res, 2)
        return np.fromiter(vals, dtype=float), [str(s) for s in states]

    # -- continuous acquisition ---------------------------------------------------------
    def acquisition(self, uuid, connectors: Sequence[int], signal: int = 0,
                    buffer_seconds: float = 4.0, synchronized: bool = False) -> "Acquisition":
        return Acquisition(self, uuid, connectors, signal, buffer_seconds, synchronized)


NTP_EPOCH_2000_UNIX = 946684800.0  # 2000-01-01 00:00:00 UTC as Unix time


@dataclass
class Chunk:
    """New samples of one signal: timestamps (s since 2000-01-01), values, value states."""
    t: np.ndarray
    values: np.ndarray
    states: np.ndarray

    def __len__(self) -> int:
        return len(self.values)

    def unix_time(self) -> np.ndarray:
        return self.t + NTP_EPOCH_2000_UNIX


class Acquisition:
    """Continuous DAQ of analog-in sync signals: subscribe -> start -> poll() ... -> stop.

        with qx.acquisition(uuid, connectors=[0, 1, 2]) as acq:
            while ...:
                chunks = acq.poll()     # {connector: Chunk(t, values, states)}
                time.sleep(0.1)

    Call poll() often enough that the device-side buffer (buffer_seconds) never overflows.
    """

    def __init__(self, qx: QuantumX, uuid, connectors: Sequence[int], signal: int = 0,
                 buffer_seconds: float = 4.0, synchronized: bool = False):
        self.synchronized = synchronized  # StartDAQ(True) aligns start of several modules
        self.qx, self.uuid, self.connectors, self.signal = qx, uuid, list(connectors), signal
        self.rates: Dict[int, float] = {}
        self._slots: Dict[int, int] = {}  # slot -> connector
        dev = qx.read_device(uuid)
        for slot, con in enumerate(self.connectors):
            sig = dev.Connectors[con].AnalogInChannel.SyncSignals[signal]
            rate = sig.OutputRate if sig.OutputRate is not None else 100.0
            self.rates[con] = float(rate)
            # Subscribe by connector/channel/signal number, as in the 2009 API demo (that API has
            # no SignalReference; the 2012 API's reference was None for this MX840).
            # Buffer: demo uses 10000 bytes; grow it for high rates (rate * seconds * sizeof(float)).
            number = int(sig.Number)
            nbytes = max(10000, int(math.ceil(rate) * buffer_seconds * 4))
            ctype = net_type("eConnectorTypes").ANALOG_IN_CHANNEL
            try:
                qx._call(qx.DAQ.SubscribeSignal, uuid, con, 0, number, ctype, slot, nbytes)
            except QuantumXError as ex:
                raise QuantumXError(
                    f"Subscribing connector {con} (signal number {number}, "
                    f"rate={rate} Hz, buffer={nbytes} bytes) failed: {ex}") from ex
            self._slots[slot] = con
        self.running = False

    def start(self) -> None:
        self.qx._call(self.qx.DAQ.StartDAQ, self.synchronized)
        self.running = True
        self.t_start = time.time()

    def poll(self) -> Dict[int, Chunk]:
        """Fetch everything acquired since the last poll: {connector: Chunk(t, values, states)}.

        t is the module's timestamp per sample. The 2012 API documents it as seconds since
        2000-01-01 00:00:00 (see Chunk.unix_time()); the 2009 API does not state the unit.
        States are "not yet supported" in the 2009 API; missing fields are filled with
        NaN (t) or 0 (states).
        """
        self.qx._call(self.qx.DAQ.GetDataBlock)
        out = {}
        for slot, con in self._slots.items():
            n, mv = _outs(self.qx.DAQ.GetSignalData(slot, None), 2)
            n = int(n)
            if n <= 0 or mv is None:
                out[con] = Chunk(np.empty(0), np.empty(0), np.empty(0, dtype=np.int32))
                continue
            out[con] = Chunk(
                t=_array(mv.TimeStamps, n, float, np.nan),
                values=np.fromiter(mv.Values, dtype=float, count=n),
                states=_array(mv.States, n, np.int32, 0),
            )
        return out

    def stop(self) -> None:
        if self.running:
            try:
                self.qx.DAQ.StopDAQ()
            finally:
                self.running = False

    def __enter__(self) -> "Acquisition":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
