"""Synchronous acquisition from one, two or more QuantumX modules (e.g. MX840) into one CSV.

Choosing modules and connectors
-------------------------------
Connector numbers are 1-based, as printed on the module.

A) Different connectors on each module: one --channels entry per module, MODULE:CONNECTORS
   (MODULE = IP, name or UUID hex; CONNECTORS = comma-separated list, ranges allowed):

    python examples/sync_record.py --channels 192.168.0.217:1 192.168.0.218:1-3
    python examples/sync_record.py --channels 192.168.0.217:1,4,5 192.168.0.218:2,7 --rate 2400

B) The same connectors on every module: --modules + --connectors

    python examples/sync_record.py --modules 192.168.0.217 192.168.0.218 --connectors 1 2 3
    python examples/sync_record.py --connectors 1 2      # every module found, connectors 1 and 2

Other options:

    python examples/sync_record.py --channels 192.168.0.217:1 192.168.0.218:1 --seconds 60 --out run.csv
    python examples/sync_record.py ... --no-sync          # free-running start, for comparison

How it works
------------
* Every module is connected over Ethernet (QXSystem.Connect).
* The same sample rate is set on all subscribed connectors of all modules (--rate), because
  one CSV row = one instant for every channel.
* All signals of all modules are subscribed to the single, global QXSimpleDAQ with a unique
  UserSignalID each, then QXSimpleDAQ.StartDAQ(True) starts every module together.
  Per the API help, with modules wired via FireWire (IEEE 1394, the sync/backplane cable)
  the first timestamps of all modules are then equal. Without the FireWire link the
  modules' clocks are independent and the start can only be approximately simultaneous.
* Each poll calls GetDataBlock once, which fetches new data from all modules at once.
* After recording, the channels are aligned on the module timestamps (samples before the
  latest common start are dropped) and cut to the shortest length.

Column names are <module name>_c<connector>; the IP is appended to the name when several
modules have the same name (e.g. MX840@192.168.0.217_c1).
"""
import argparse
import csv
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyquantumx import QuantumX, QuantumXError, net_type  # noqa: E402
from pyquantumx.quantumx import NTP_EPOCH_2000_UNIX, _array, _outs  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                             epilog="example: --channels 192.168.0.217:1,2 192.168.0.218:1-3")
ap.add_argument("--channels", nargs="+", metavar="MODULE:CONNECTORS",
                help="per-module connectors, e.g. 192.168.0.217:1,2 192.168.0.218:1-3 "
                     "(overrides --modules/--connectors)")
ap.add_argument("--modules", nargs="+", help="IPs / names / UUID hex (default: every module found)")
ap.add_argument("--adapter", type=int, nargs="*", help="network adapter index(es) to scan (see check_env.py)")
ap.add_argument("--connectors", type=int, nargs="+", default=list(range(1, 9)),
                help="1-based connectors recorded on EVERY module given by --modules (default: 1..8)")
ap.add_argument("--rate", type=float, default=1200.0, help="sample rate in Hz set on all modules")
ap.add_argument("--seconds", type=float, default=10.0)
ap.add_argument("--poll", type=float, default=0.1, help="poll interval in s")
ap.add_argument("--buffer", type=float, default=4.0, help="per-signal API buffer in seconds of data")
ap.add_argument("--no-sync", action="store_true", help="use StartDAQ(False) instead of a synchronized start")
ap.add_argument("--out", default="sync_recording.csv")
args = ap.parse_args()


def parse_connectors(text):
    """'1,3-5' -> [0, 2, 3, 4] (0-based for the API)."""
    out = []
    for part in text.split(","):
        a, _, b = part.strip().partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    if not out or min(out) < 1:
        raise ValueError(text)
    return [c - 1 for c in dict.fromkeys(out)]


# selection = list of (module target, [0-based connectors])
if args.channels:
    selection = []
    for spec in args.channels:
        module, sep, cons_text = spec.rpartition(":")
        try:
            if not sep or not module:
                raise ValueError(spec)
            selection.append((module, parse_connectors(cons_text)))
        except ValueError:
            ap.error(f"bad --channels entry {spec!r}; use MODULE:CONNECTORS, e.g. 192.168.0.217:1,2,5-8")
else:
    selection = None  # filled after the scan from --modules / --connectors


def label(info):
    return info.name if info.name and info.name.strip() else info.ip


with QuantumX() as qx:
    # ---- 1. find and connect -------------------------------------------------------------
    found = qx.scan(args.adapter or None)
    print("Found:", *(f"  {s}" for s in found), sep="\n")
    if selection is None:
        cons_all = [c - 1 for c in args.connectors]
        selection = [(t, cons_all) for t in (args.modules or [s.ip for s in found])]
    if not selection:
        sys.exit("No QuantumX module found.")
    devs = [qx.connect(t) for t, _ in selection]
    module_cons = [cons for _, cons in selection]
    # module names are often identical ("MX840"); make labels unique with the IP
    names = [label(d) for d in devs]
    labels = [f"{n}@{d.ip}" if names.count(n) > 1 else n for n, d in zip(names, devs)]
    for lab, d, cons in zip(labels, devs, module_cons):
        print(f"Connected: {lab:24} {d.ip}  UUID={d.uuid_hex}  connectors={[c + 1 for c in cons]}")

    # ---- 2. same sample rate everywhere -------------------------------------------------
    rates = {}
    for lab, d, cons in zip(labels, devs, module_cons):
        n_con = len(qx.read_device(d.uuid).Connectors)
        bad = [c + 1 for c in cons if c >= n_con]
        if bad:
            sys.exit(f"{lab} has only {n_con} connectors; cannot use {bad}.")
        for c, r in qx.set_sample_rate(d.uuid, args.rate, cons).items():
            rates[(lab, c)] = r
    uniq = sorted(set(rates.values()))
    if len(uniq) != 1:
        sys.exit(f"Channels ended up with different rates {uniq} Hz; cannot write one common time base.")
    rate = uniq[0]
    print(f"Sample rate on all channels: {rate} Hz")

    # ---- 3. subscribe all signals of all modules (one global DAQ, unique IDs) ------------
    ctype = net_type("eConnectorTypes").ANALOG_IN_CHANNEL
    nbytes = max(10000, int(math.ceil(rate) * args.buffer * 4))
    channels = []  # (UserSignalID, module label, connector index)
    for lab, d, cons in zip(labels, devs, module_cons):
        dev_obj = qx.read_device(d.uuid)
        for c in cons:
            number = int(dev_obj.Connectors[c].AnalogInChannel.SyncSignals[0].Number)
            sid = len(channels)
            try:
                qx._call(qx.DAQ.SubscribeSignal, d.uuid, c, 0, number, ctype, sid, nbytes)
            except QuantumXError as ex:
                sys.exit(f"Subscribing {lab} connector {c + 1} failed: {ex}")
            channels.append((sid, lab, c))
    print(f"Subscribed {len(channels)} signals from {len(devs)} module(s)")

    # ---- 4. acquire ------------------------------------------------------------------------
    t_parts = {sid: [] for sid, _, _ in channels}
    v_parts = {sid: [] for sid, _, _ in channels}

    def collect():
        qx._call(qx.DAQ.GetDataBlock)  # one call fetches the new data of all modules
        for sid, _, _ in channels:
            n, mv = _outs(qx.DAQ.GetSignalData(sid, None), 2)
            n = int(n)
            if n > 0 and mv is not None:
                t_parts[sid].append(_array(mv.TimeStamps, n, float, np.nan))
                v_parts[sid].append(np.fromiter(mv.Values, dtype=float, count=n))

    sync = not args.no_sync
    qx._call(qx.DAQ.StartDAQ, sync)
    print(f"DAQ started ({'synchronized' if sync else 'NOT synchronized'}), recording {args.seconds} s ...")
    try:
        t_end = time.time() + args.seconds
        while time.time() < t_end:
            collect()
            got = sum(len(a) for a in v_parts[channels[0][0]])
            print(f"\r  {got} samples per channel", end="", flush=True)
            time.sleep(args.poll)
        collect()  # drain what is left
    finally:
        qx.DAQ.StopDAQ()
    print()

    # ---- 5. align on module timestamps and write ------------------------------------------
    T = {sid: np.concatenate(p) if p else np.empty(0) for sid, p in t_parts.items()}
    V = {sid: np.concatenate(p) if p else np.empty(0) for sid, p in v_parts.items()}
    if min(len(v) for v in V.values()) == 0:
        sys.exit("At least one channel received no samples.")

    have_ts = not any(np.isnan(t[0]) for t in T.values())
    dt = 1.0 / rate
    if have_ts:
        print("First timestamp per module (s since module clock epoch):")
        firsts = {}
        for sid, lab, c in channels:
            firsts.setdefault(lab, T[sid][0])
        for lab, t0 in firsts.items():
            print(f"  {lab:24} {t0:.6f}")
        spread = max(firsts.values()) - min(firsts.values())
        print(f"  spread = {spread * 1e3:.3f} ms ({spread / dt:.1f} samples)")
        if spread > dt * 1.5:
            print("  WARNING: modules do not start on the same timestamp. Their clocks are probably\n"
                  "  not synchronized (no FireWire link between the modules?). Rows are aligned by\n"
                  "  each module's own clock, which may be offset from the others.")
        # drop samples before the latest common start, so row i is the same instant everywhere
        t_start = max(t[0] for t in T.values())
        for sid in T:
            k = int(np.searchsorted(T[sid], t_start - dt / 2))
            T[sid], V[sid] = T[sid][k:], V[sid][k:]
    else:
        print("No module timestamps available; aligning by sample index only.")

    n = min(len(v) for v in V.values())
    ref = channels[0][0]
    t = (T[ref][:n] - T[ref][0]) if have_ts else np.arange(n) * dt
    if have_ts:
        try:
            start = datetime.fromtimestamp(T[ref][0] + NTP_EPOCH_2000_UNIX, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            start = "unknown"
    else:
        start = "unknown"

    with open(args.out, "w", newline="") as f:
        f.write(f"# start={start} (module clock)  rate={rate} Hz  synchronized_start={sync}  "
                f"modules={' '.join(f'{lab}={d.ip}' for lab, d in zip(labels, devs))}\n")
        w = csv.writer(f)
        w.writerow(["time_s"] + [f"{lab}_c{c + 1}" for _, lab, c in channels])
        for i in range(n):
            w.writerow([f"{t[i]:.6f}"] + [f"{V[sid][i]:.6g}" for sid, _, _ in channels])
    print(f"Wrote {n} rows x {len(channels)} channels ({n / rate:.2f} s) to {args.out}")
