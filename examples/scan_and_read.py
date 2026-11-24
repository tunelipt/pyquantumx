"""Connect to a module, show its configuration and print live values a few times.

    python examples/scan_and_read.py              # first module found
    python examples/scan_and_read.py 192.168.1.20  # by IP, name or UUID hex
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qxpy import QuantumX  # noqa: E402

with QuantumX() as qx:
    found = qx.scan()
    if not found:
        sys.exit("No QuantumX module found.")
    dev = qx.connect(sys.argv[1] if len(sys.argv) > 1 else found[0])
    print("Connected:", dev)

    for m in qx.modules():
        print(f"  module {m.uuid_hex}  type={m.device_type}" + (f"  (via {m.parent})" if m.parent else ""))

    print("\nConnectors (sync-signal output rates in Hz):")
    for row in qx.describe(dev.uuid):
        print(f"  #{row['connector'] + 1}: {row['rates_hz']}")

    # full configuration as XML, handy for finding sensor/channel names and units
    Path("device_config.xml").write_text(qx.device_xml(dev.uuid), encoding="utf-8")
    print("\nFull configuration written to device_config.xml")

    print("\nLive values (GetSingleShot):")
    for _ in range(5):
        values, states = qx.read_all(dev.uuid)
        print("  ", " ".join(f"{v:10.4f}" for v in values))
        time.sleep(0.5)
