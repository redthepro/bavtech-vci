"""Best-effort remote reset of the BavTech VCI cable.

1. USB port reset (USBDEVFS_RESET ioctl) — re-enumerates the device.
NOTE: do NOT use the firmware soft-reset command (0x01) — it leaves the
MCU half-booted (empty responses) until another USB reset.
Also note: if the OBD end is plugged into a vehicle, the MCU stays powered
from battery pin 16 and only a physical unplug fully power-cycles it.
"""
import fcntl, glob, os, sys, time

VID, PID = "1fc9", "8084"
USBDEVFS_RESET = 21780

def find_devnode():
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(dev + "/idVendor") as f: v = f.read().strip()
            with open(dev + "/idProduct") as f: p = f.read().strip()
        except (OSError, IOError):
            continue
        if v == VID and p == PID:
            with open(dev + "/busnum") as f: bus = int(f.read())
            with open(dev + "/devnum") as f: num = int(f.read())
            return f"/dev/bus/usb/{bus:03d}/{num:03d}"
    return None

node = find_devnode()
if not node:
    print("cable not found on USB"); sys.exit(1)
print(f"resetting {node} ...")
fd = os.open(node, os.O_WRONLY)
try:
    fcntl.ioctl(fd, USBDEVFS_RESET, 0)
finally:
    os.close(fd)
time.sleep(2.5)

node2 = find_devnode()
print(f"re-enumerated at {node2}" if node2 else "device did not come back!")
if not node2:
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kia_vci import Vci
import kia_vci
for attempt in range(4):
    try:
        vci = Vci()
    except OSError:
        time.sleep(1.5); continue
    fw = vci.firmware_version()
    vci.close()
    if fw:
        print("cable alive, firmware:", fw); break
    time.sleep(1.5)
else:
    print("cable not responding after reset"); sys.exit(1)
print("done")
