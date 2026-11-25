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


_enum_hz_cache: Dict[str, List[float]] = {}


def _internal_enum_hz(name: str) -> List[float]:
    """Values (Hz, ascending) of an internal HBM.DeviceComponents enum stored in mHz."""
    if name not in _enum_hz_cache:
        import clr  # noqa: E402
        import System  # type: ignore  # noqa: E402

        asm = clr.GetClrType(load_api().dc.SyncSignal).Assembly
        t = asm.GetType(f"HBM.DeviceComponents.{name}")
        if t is None:
            raise QuantumXError(f"HBM.DeviceComponents.{name} not found in the API DLL")
        _enum_hz_cache[name] = sorted(System.Convert.ToInt64(v) / 1000.0 for v in System.Enum.GetValues(t))
    return list(_enum_hz_cache[name])


def _nearest(values: Sequence[float], hz: float, what: str) -> float:
    """The value closest to hz, compared on a log scale."""
    if hz <= 0:
        raise ValueError(f"{what} must be positive, got {hz}")
    return min(values, key=lambda v: abs(math.log(v / hz)))


def sample_rates() -> List[float]:
    """All output rates (Hz) the API knows, ascending, from HBM.DeviceComponents.eOutputRates.

    The enum is internal, so it is read by reflection; its values are in mHz. This is one list
    for every module type: a given module supports only some of them (an MX840 tops out at
    19200 Hz), and the API has no per-module query.
    """
    return _internal_enum_hz("eOutputRates")


def nearest_sample_rate(rate_hz: float) -> float:
    """The rate in sample_rates() closest to rate_hz (compared on a log scale)."""
    return _nearest(sample_rates(), rate_hz, "Sample rate")


def filter_frequencies() -> List[float]:
    """All low-pass cut-off frequencies (Hz) the API knows, ascending (0.01 Hz ... 20 kHz).

    Read from the internal enum HBM.DeviceComponents.eFilterFrequencies (values in mHz).
    One list for every module type; the API has no per-module query.
    """
    return _internal_enum_hz("eFilterFrequencies")


def nearest_filter_frequency(cutoff_hz: float) -> float:
    """The cut-off in filter_frequencies() closest to cutoff_hz (compared on a log scale)."""
    return _nearest(filter_frequencies(), cutoff_hz, "Filter frequency")


def _filter_info(sig) -> Dict:
    """{'type', 'characteristic', 'cutoff_hz'} of a SyncSignal's filter (None where not set)."""
    ft = sig.FilterType
    if ft is None:
        return {"type": None, "characteristic": None, "cutoff_hz": None}
    lp = ft.LowPass
    on = str(ft.ActiveType) == "LOWPASS"
    return {
        "type": str(ft.ActiveType),
        "characteristic": str(lp.FilterCharacteristic) if on and lp is not None else None,
        "cutoff_hz": float(lp.FilterFrequency) if on and lp is not None and lp.FilterFrequency is not None else None,
    }


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
        """Short summary per connector (0-based) and its analog-in sync signals.

        Each row: {'connector', 'type', 'rates_hz', 'signal_numbers', 'signals'}, where
        'signals' holds one dict per SyncSignal: number, name, rate_hz, unit, filter
        (see set_filter). rates_hz / signal_numbers repeat those fields as lists.
        """
        dev = self.read_device(uuid)
        rows = []
        for i, con in enumerate(dev.Connectors):
            row = {"connector": i, "type": str(con.ActiveType), "rates_hz": [], "signal_numbers": [], "signals": []}
            ain = getattr(con, "AnalogInChannel", None)
            if ain is not None and getattr(ain, "SyncSignals", None) is not None:
                for s in ain.SyncSignals:
                    unit = str(s.OutputUnitUser) if s.OutputUnitUser else str(s.OutputUnit)
                    row["signals"].append({
                        "number": int(s.Number),
                        "name": str(s.SignalName) if s.SignalName is not None else None,
                        "rate_hz": float(s.OutputRate) if s.OutputRate is not None else None,
                        "unit": unit,
                        "filter": _filter_info(s),
                    })
                row["rates_hz"] = [d["rate_hz"] for d in row["signals"]]
                row["signal_numbers"] = [d["number"] for d in row["signals"]]
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

    # -- sensors --------------------------------------------------------------------------
    def read_sensor(self, uuid, connector: int):
        """The .NET Sensor object on a connector (0-based); .ConvertToXML() shows all settings."""
        return self._call(self.S.ReadSensor, uuid, connector)

    def set_sensor(self, uuid, connector: int, sensor) -> object:
        """Assign a .NET Sensor object to a connector (0-based) and return what the module stored.

        Switches the connector to analog input first if it isn't one already (AssignSensor
        fails on an OFF or CAN bus connector). AssignSensor replaces the whole sensor,
        scaling included. If the module only adjusts values (AssignFailed with warnings
        only), a Python warning is issued and the adjusted sensor is returned; real errors
        raise QuantumXError.
        """
        ctypes_ = net_type("eConnectorTypes")
        con = self.read_device(uuid).Connectors[connector]
        if con.ActiveType != ctypes_.ANALOG_IN_CHANNEL:
            self._call(self.S.AssignConnectorType, uuid, connector, ctypes_.ANALOG_IN_CHANNEL)
        self._assign(self.S.AssignSensor, uuid, connector, sensor)
        return self.read_sensor(uuid, connector)

    def _assign(self, fn, *args) -> List[str]:
        """Call an Assign* method. AssignFailed with warnings only (the module adjusted a value)
        becomes a Python warning and returns the warning texts; real errors raise QuantumXError."""
        name = getattr(fn, "__name__", str(fn))
        try:
            fn(*args)
            return []
        except Exception as ex:
            probs = list(self.S.ReadLastProblems())
            if not (probs and all(str(p.Type).lower().startswith("warn") for p in probs)):
                details = "; ".join(f"{p.Type}: {p.Text}" for p in probs)
                raise QuantumXError(f"{name} failed: {ex}" + (f" | {details}" if details else "")) from ex
            texts = [str(p.Text) for p in probs]
            warnings.warn(f"{name} adjusted settings: " + "; ".join(texts), stacklevel=3)
            return texts

    # -- signal filters ---------------------------------------------------------------------
    def set_filter(self, uuid, cutoff_hz: Optional[float], characteristic: str = "bessel",
                   connectors: Optional[Iterable[int]] = None,
                   signals: Optional[Iterable[int]] = None) -> Dict[tuple, Dict]:
        """Set the low-pass filter of analog-in sync signals.

        Returns {(connector, signal Number): {'type', 'characteristic', 'cutoff_hz'}}.

        cutoff_hz: cut-off frequency in Hz, rounded to the nearest value in
        filter_frequencies() (with a warning if it changes); None turns the filter off.
        characteristic: 'bessel' or 'butterworth'.
        connectors: 0-based connector numbers (default: all analog-in connectors).
        signals: indices into each channel's SyncSignals (default: all of them).
        Each signal is read with ReadSyncSignal, so its other settings (rate, unit, ...) are
        kept, and written back with AssignSyncSignal. The returned dicts come from reading the
        signals back, so they show what the module actually stored.
        """
        ftypes = net_type("eFilterTypes")
        fchar = getattr(net_type("eFilterCharacteristics"), characteristic.upper())
        if cutoff_hz is not None:
            requested = float(cutoff_hz)
            cutoff_hz = nearest_filter_frequency(requested)
            if cutoff_hz != requested:
                warnings.warn(f"{requested} Hz is not an API filter frequency; using {cutoff_hz} Hz.", stacklevel=2)

        dev = self.read_device(uuid)
        idx = range(len(dev.Connectors)) if connectors is None else connectors
        targets = []
        for c in idx:
            ain = dev.Connectors[c].AnalogInChannel
            if ain is None or ain.SyncSignals is None:
                if connectors is not None:
                    raise QuantumXError(f"Connector {c} is not an analog input")
                continue
            n = len(ain.SyncSignals)
            for j in (range(n) if signals is None else signals):
                if j < n:
                    targets.append((c, int(ain.SyncSignals[j].Number)))

        out = {}
        for c, number in targets:
            sig = self._call(self.S.ReadSyncSignal, uuid, c, number)
            if cutoff_hz is None:
                sig.FilterType.ActiveType = ftypes.OFF
            else:
                sig.FilterType.ActiveType = ftypes.LOWPASS
                sig.FilterType.LowPass.FilterCharacteristic = fchar
                sig.FilterType.LowPass.FilterFrequency = float(cutoff_hz)
            self._assign(self.S.AssignSyncSignal, uuid, c, sig)
            out[(c, number)] = _filter_info(self.S.ReadSyncSignal(uuid, c, number))
        return out

    def _new_sensor(self, kind: str, zero_span: Optional[Sequence[float]]):
        s = self.api.dc.Sensor()
        s.SensorType.ActiveType = getattr(net_type("eSensorTypes"), kind)
        scal = net_type("eScalingTypes")
        if zero_span is None:
            s.ScalingType.ActiveType = scal.OFF
        else:
            ez, es, ps = (float(v) for v in zero_span)
            s.ScalingType.ActiveType = scal.ZEROSPAN
            s.ScalingType.ZeroSpan.ElectricalZero = ez
            s.ScalingType.ZeroSpan.ElectricalSpan = es
            s.ScalingType.ZeroSpan.PhysicalSpan = ps
        return s

    def set_bridge(self, uuid, connector: int, bridge: str = "full", wiring: int = 6,
                   excitation: float = 2.5, max_electrical: Optional[float] = None,
                   excite_frequency: Optional[str] = None,
                   zero_span: Optional[Sequence[float]] = None) -> object:
        """Make a connector (0-based) a bridge input (strain gauge / load cell transducer).

        bridge: 'full', 'half' or 'quarter'. wiring: 2..6 wires. excitation: volts (the API
        lists 1 and 2.5 V). max_electrical: measuring range, presumably in mV/V (module default
        if None). excite_frequency: 'AUTO', 'DC', 'Hz_75', 'Hz_225', 'Hz_600', 'Hz_4800' or
        'Hz_9600' (module default if None).
        zero_span: (electrical_zero, electrical_span, physical_span) for ZEROSPAN scaling,
        e.g. (0, 2, 100) maps 0..2 mV/V to 0..100. None = no scaling (electrical value).
        Zero the input afterwards with zero(). Returns the Sensor the module stored.
        """
        s = self._new_sensor("BRIDGE", zero_span)
        b = s.SensorType.Bridge
        b.BridgeType = getattr(net_type("eBridgeTypes"), bridge.upper())
        b.Wiring = getattr(net_type("eWirings"), f"WIRE_{int(wiring)}")
        b.ExciteAmplitude = float(excitation)
        if max_electrical is not None:
            b.MaxElectrical = float(max_electrical)
        if excite_frequency is not None:
            ef = excite_frequency.strip()
            ef = "Hz_" + ef[3:] if ef.lower().startswith("hz_") else ef.upper()
            b.ExciteFrequency = getattr(net_type("eExcitationFrequencies"), ef)
        return self.set_sensor(uuid, connector, s)

    def set_voltage(self, uuid, connector: int, max_electrical: Optional[float] = None,
                    zero_span: Optional[Sequence[float]] = None) -> object:
        """Make a connector (0-based) a voltage input.

        max_electrical: measuring range in V (module default if None).
        zero_span: (electrical_zero, electrical_span, physical_span) for ZEROSPAN scaling,
        e.g. (0, 10, 500) maps 0..10 V to 0..500. None = no scaling (volts).
        Returns the Sensor the module stored.
        """
        s = self._new_sensor("VOLTAGE", zero_span)
        if max_electrical is not None:
            s.SensorType.Voltage.MaxElectrical = float(max_electrical)
        return self.set_sensor(uuid, connector, s)

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
