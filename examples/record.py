"""Record analog-in channels continuously to a CSV file.

    python examples/record.py --connectors 1 2 3 --rate 1200 --seconds 10 --out run1.csv
    python examples/record.py --module 192.168.1.20 --adapter 1 --connectors 1 --seconds 5

Connector numbers are as printed on the module (1-based). The time column comes from the
module's own per-sample timestamps (taken from the first channel), relative to the first
sample; the absolute UTC start time is written in the first line of the file.
"""
import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pyquantumx import QuantumX  # noqa: E402
from pyquantumx.quantumx import NTP_EPOCH_2000_UNIX  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--module", help="IP, name or UUID hex (default: first found)")
ap.add_argument("--adapter", type=int, nargs="*", help="network adapter index(es) to scan (see check_env.py)")
ap.add_argument("--connectors", type=int, nargs="+", default=[1], help="1-based connector numbers")
ap.add_argument("--rate", type=float, help="set this sample rate (Hz) on the module first")
ap.add_argument("--seconds", type=float, default=10.0)
ap.add_argument("--poll", type=float, default=0.1, help="poll interval in s")
ap.add_argument("--out", default="recording.csv")
args = ap.parse_args()
cons = [c - 1 for c in args.connectors]  # API is 0-based

with QuantumX() as qx:
    found = qx.scan(args.adapter or None)
    if not found:
        sys.exit("No QuantumX module found.")
    dev = qx.connect(args.module or found[0])
    print("Connected:", dev)

    if args.rate:
        qx.set_sample_rate(dev.uuid, args.rate, cons)

    t_parts, v_parts = [], {c: [] for c in cons}
    with qx.acquisition(dev.uuid, cons) as acq:
        print("Rates (Hz):", {c + 1: r for c, r in acq.rates.items()})

        def collect():
            chunks = acq.poll()
            t_parts.append(chunks[cons[0]].t)
            for c in cons:
                v_parts[c].append(chunks[c].values)

        t_end = time.time() + args.seconds
        while time.time() < t_end:
            collect()
            print(f"\r  {sum(len(a) for a in t_parts)} samples", end="", flush=True)
            time.sleep(args.poll)
        collect()  # drain the rest
    print()

    t = np.concatenate(t_parts) if t_parts else np.empty(0)
    cols = {c: np.concatenate(v) for c, v in v_parts.items()}
    n = min([len(t)] + [len(a) for a in cols.values()])
    if n == 0:
        sys.exit("No samples received.")

    rate = acq.rates[cons[0]]
    if np.isnan(t[:n]).any():
        # no usable module timestamps (e.g. older API): use sample index / rate
        t = np.arange(n) / rate
        header = f"# time from sample index at {rate} Hz  module={dev.name}"
    else:
        try:
            start = datetime.fromtimestamp(t[0] + NTP_EPOCH_2000_UNIX, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            start = "unknown"
        header = f"# start_utc={start} (if timestamps are NTP seconds since 2000)  rate={rate} Hz  module={dev.name}"
    t0 = t[0]
    with open(args.out, "w", newline="") as f:
        f.write(header + "\n")
        w = csv.writer(f)
        w.writerow(["time_s"] + [f"connector_{c + 1}" for c in cons])
        for i in range(n):
            w.writerow([f"{t[i] - t0:.6f}"] + [f"{cols[c][i]:.6g}" for c in cons])
    print(f"Wrote {n} rows x {len(cons)} channels to {args.out}")
