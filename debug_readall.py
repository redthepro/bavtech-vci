"""Compare CAN_READ vs CAN_READ_ALL raw payloads after an OBD query."""
import time
from vci import Vci, hexs, CAN_READ, CAN_READ_ALL, OBD_REQUEST_ID

vci = Vci()
vci.can_config()

print("--- query 0100, then 10x CAN_READ_ALL raw ---")
vci.can_write(OBD_REQUEST_ID, bytes([2, 0x01, 0x00]))
for _ in range(10):
    _, p = vci.xfer(CAN_READ_ALL, timeout_ms=200)
    print(f"len={len(p)}: {hexs(p[:48])}")

print("--- query 0100, then 30x CAN_READ raw (fast) ---")
vci.can_write(OBD_REQUEST_ID, bytes([2, 0x01, 0x00]))
hits = 0
for _ in range(60):
    _, p = vci.xfer(CAN_READ, timeout_ms=100)
    if len(p) >= 11:
        cid = (p[2] << 8) | p[1]
        if 0x7E8 <= cid <= 0x7EF:
            hits += 1
            print(f"OBD: {hexs(p)}")
print(f"obd hits: {hits}")

vci.can_stop()
vci.close()
