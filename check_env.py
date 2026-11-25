"""Step 1: verify Python bitness, pythonnet and the HBM DLLs, then list adapters and scan.

    python check_env.py [path\\to\\QuantumX API\\DLLs]
"""
import platform
import struct
import sys

print(f"Python {platform.python_version()}  {struct.calcsize('P') * 8}-bit  ({sys.executable})")

try:
    import pythonnet  # noqa: F401
    print("pythonnet: OK")
except ImportError:
    sys.exit("pythonnet missing -> pip install -r requirements.txt")

from pyquantumx import QuantumX, QuantumXError

try:
    with QuantumX(sys.argv[1] if len(sys.argv) > 1 else None) as qx:
        print(f"API DLL dir: {qx.api.dir}")
        print(f"HBM_QX_Framework version: {qx.dll_version()}")
        print("\nNetwork adapters:")
        adapters = qx.network_adapters()
        for i, a in enumerate(adapters):
            print(f"  [{i}] {a['ip']:16} {a['description']}")
        print("\nScanning for QuantumX modules (default adapter)...")
        found = qx.scan()
        if not found and len(adapters) > 1:
            print("Nothing on the default adapter, scanning all adapters...")
            found = qx.scan(list(range(len(adapters))))
        for s in found:
            print(f"  {s}")
        print(f"{len(found)} module(s) found.")
except QuantumXError as ex:
    sys.exit(f"ERROR: {ex}")
