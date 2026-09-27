"""Reset cable MCU, reconfig CAN, verify bus chatter and OBD response."""
import time
from vci import Vci, hexs, CAN_READ, OBD_REQUEST_ID

RESET = 0x01

vci = Vci()
print("RESET:", vci.xfer(RESET, timeout_ms=1500))
time.sleep(1.5)
try:
    vci.close()
except OSError:
    pass
time.sleep(1.0)
vci = Vci()  # reopen in case it re-enumerated
print("firmware after reset:", vci.firmware_version())
vci.can_config()

print("--- 2s of raw CAN_READ ---")
t0 = time.time()
n = 0
obd = 0
while time.time() - t0 < 2:
    _, p = vci.xfer(CAN_READ, timeout_ms=100)
    if len(p) >= 11 and any(p):
        n += 1
        cid = (p[2] << 8) | p[1]
        if 0x7E8 <= cid <= 0x7EF:
            obd += 1
print(f"frames: {n} (obd: {obd})")

print("--- query 0100, poll CAN_READ 3s ---")
vci.can_write(OBD_REQUEST_ID, bytes([2, 0x01, 0x00]))
t0 = time.time()
while time.time() - t0 < 3:
    _, p = vci.xfer(CAN_READ, timeout_ms=100)
    if len(p) >= 11:
        cid = (p[2] << 8) | p[1]
        if 0x7E8 <= cid <= 0x7EF:
            print(f"OBD RESPONSE: {hexs(p)}")

vci.can_stop()
vci.close()
