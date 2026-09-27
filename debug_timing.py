"""Test whether CAN_CONFIG needs settle time before CAN_WRITE is heard."""
import time
from vci import Vci, hexs, OBD_REQUEST_ID

def poll_for_obd(vci, seconds):
    t0 = time.time()
    hits = []
    frames = 0
    while time.time() - t0 < seconds:
        cid, d = vci.can_read_frame()
        if cid is None:
            continue
        frames += 1
        if 0x7E8 <= cid <= 0x7EF:
            hits.append((time.time() - t0, cid, bytes(d)))
    return frames, hits

vci = Vci()
print("firmware:", vci.firmware_version())

for settle in (0.0, 0.2, 1.0):
    vci.can_stop()
    vci.can_config()
    time.sleep(settle)
    vci.can_write(OBD_REQUEST_ID, bytes([2, 0x01, 0x00]))
    frames, hits = poll_for_obd(vci, 4)
    print(f"settle={settle}s: {frames} frames, {len(hits)} OBD hits")
    for t, cid, d in hits:
        print(f"   t={t:.2f}s 0x{cid:03X}: {hexs(d)}")

vci.can_stop()
vci.close()
