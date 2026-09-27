"""Raw debugging: ADC payload, filter behavior, and OBD responses."""
import time
from kia_vci import (Vci, hexs, ADC, CAN_READ, OBD_REQUEST_ID)

vci = Vci()
print("firmware:", vci.firmware_version())

cmd, p = vci.xfer(ADC)
print(f"ADC raw -> cmd={None if cmd is None else hex(cmd)} payload: {hexs(p)}")

vci.can_config()

print("\n--- no filters, query 0100, dump raw CAN_READ payloads 3s ---")
vci.can_write(OBD_REQUEST_ID, bytes([2, 0x01, 0x00]))
t0 = time.time()
while time.time() - t0 < 3:
    _, pay = vci.xfer(CAN_READ, timeout_ms=200)
    if pay and any(pay):
        cid = (pay[2] << 8) | pay[1] if len(pay) >= 3 else -1
        mark = " <<< OBD" if 0x7E8 <= cid <= 0x7EF else ""
        print(f"  raw: {hexs(pay)}{mark}")

print("\n--- add filter 0x7E8 only, query 0100 again ---")
cmd, p = vci.can_add_filter(0x7E8)
print(f"CAN_ADD_FILTER resp: cmd={None if cmd is None else hex(cmd)} payload: {hexs(p)}")
vci.can_write(OBD_REQUEST_ID, bytes([2, 0x01, 0x00]))
t0 = time.time()
while time.time() - t0 < 3:
    _, pay = vci.xfer(CAN_READ, timeout_ms=200)
    if pay and any(pay):
        print(f"  raw: {hexs(pay)}")

vci.can_stop()
vci.close()
