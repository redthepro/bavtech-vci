"""
Drive a Bavarian Technic "Diag Tech USB Vehicle Interface" (NXP HID
generation, VID 1FC9 PID 8084) as a generic CAN/OBD-II tool.
Works on any ISO 15765-4 500k/11-bit vehicle (2018 Kia Soul,
2023 Nissan Rogue, 2023 Mazda CX-50, ...).

Protocol reverse-engineered from VCI4.dll for interoperability with the
owner's own cable/vehicles; verified live against the cable and the Soul.

Transport: 64-byte HID reports (report ID 0), VCI frame [LEN][CMD][PAYLOAD],
LEN = len(PAYLOAD)+1, responses identical.

Received CAN frame layout (observed live):
    [status][id_lo][id_hi][data x8][extra][0x5B]
"""

import argparse
import sys
import time

import hid

VID, PID = 0x1FC9, 0x8084
REPORT_SIZE = 64

ADC = 0x0A
FIRMWARE_VER = 0x02
SERIAL_NUM = 0x03
GET_PROD = 0x40
CAN_CONFIG = 0x90
CAN_ADD_FILTER = 0x91
CAN_READ = 0x92
CAN_WRITE = 0x93
CAN_STOP = 0x94
CAN_READ_ALL = 0x95

CAN_PRESET_500K = 3

OBD_REQUEST_ID = 0x7DF
ECM_REQUEST_ID = 0x7E0


def hexs(b):
    return " ".join(f"{x:02X}" for x in b) if b else "(empty)"


class Vci:
    def __init__(self):
        self.dev = hid.device()
        self.dev.open(VID, PID)
        self.dev.set_nonblocking(0)

    def close(self):
        self.dev.close()

    def _drain(self):
        while self.dev.read(REPORT_SIZE, timeout_ms=5):
            pass

    def xfer(self, cmd, payload=b"", timeout_ms=800, drain=True):
        frame = bytes([len(payload) + 1, cmd]) + bytes(payload)
        if drain:
            self._drain()
        self.dev.write(bytes([0x00]) + frame + bytes(REPORT_SIZE - len(frame)))
        buf = b""
        need = None
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            chunk = self.dev.read(REPORT_SIZE, timeout_ms=50)
            if not chunk:
                continue
            buf += bytes(chunk)
            if need is None and buf:
                need = buf[0] + 1
            if need is not None and len(buf) >= need:
                msg = buf[:need]
                return msg[1], msg[2:]
        return None, b""

    # --- identity / health ---------------------------------------------
    def firmware_version(self):
        _, p = self.xfer(FIRMWARE_VER)
        return p.rstrip(b"\x00").decode("ascii", "replace")

    def voltage(self):
        """Vehicle battery voltage measured at OBD pin 16.

        This firmware returns [status][mV_lo][mV_hi] (observed: 0A 16 2D
        = 11.542 V with ignition on).
        """
        _, p = self.xfer(ADC)
        if len(p) >= 3:
            return int.from_bytes(p[1:3], "little") / 1000.0
        return None

    # --- CAN -------------------------------------------------------------
    def can_config(self, preset=CAN_PRESET_500K):
        return self.xfer(CAN_CONFIG, bytes([preset]))

    def can_add_filter(self, can_id):
        return self.xfer(CAN_ADD_FILTER, int(can_id).to_bytes(4, "little"))

    def can_write(self, can_id, data):
        data = bytes(data)
        if len(data) < 8:
            data = data + bytes(8 - len(data))
        body = bytes([len(data) + 3]) + int(can_id).to_bytes(2, "little") + data
        return self.xfer(CAN_WRITE, body)

    def can_read_frame(self, timeout_ms=100):
        """Return (can_id, data8) or (None, b''). No pre-drain: fast poll."""
        _, p = self.xfer(CAN_READ, timeout_ms=timeout_ms, drain=False)
        if len(p) >= 11:
            can_id = (p[2] << 8) | p[1]
            return can_id, p[3:11]
        return None, b""

    def can_flush(self):
        """Clear the cable's CAN RX FIFO by re-initializing the controller."""
        self.can_stop()
        self.can_config()

    def can_stop(self):
        return self.xfer(CAN_STOP)


# --- ISO-TP over the cable's frame API -----------------------------------

def isotp_recv(vci, timeout_s=2.0):
    """Receive one ISO-TP message from any 0x7E8-0x7EF responder.

    Software-filters the bus flood (the cable's hardware filter is buggy).
    Sends flow-control to the matching physical request ID when a first
    frame arrives. Returns (resp_id, payload) or (None, b'').
    """
    first_id = None
    expected = 0
    data = b""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        can_id, d = vci.can_read_frame()
        if can_id is None or not (0x7E8 <= can_id <= 0x7EF) or len(d) < 8:
            continue
        pci = d[0] >> 4
        if pci == 0 and first_id is None:            # single frame
            n = d[0] & 0x0F
            if 1 <= n <= 7:
                return can_id, bytes(d[1 : 1 + n])
        elif pci == 1 and first_id is None:          # first frame
            first_id = can_id
            expected = ((d[0] & 0x0F) << 8) | d[1]
            data = bytes(d[2:8])
            # flow control: continue, no block limit, no separation time
            vci.can_write(can_id - 8, b"\x30\x00\x00")
        elif pci == 2 and can_id == first_id:        # consecutive frame
            data += bytes(d[1:8])
            if len(data) >= expected:
                return first_id, data[:expected]
    return (first_id, data[:expected]) if first_id and data else (None, b"")


def obd_request(vci, mode, pid=None, timeout_s=3.0):
    """Send an OBD request and wait for a *matching* response.

    No CAN_STOP/CAN_CONFIG here: repeated stop/start cycles wedge the
    cable's CAN controller until a hard power cycle. We simply poll fast
    and match the reply by mode (+0x40) and PID, ignoring stale frames.
    """
    req = bytes([2, mode, pid]) if pid is not None else bytes([1, mode])
    vci.can_write(OBD_REQUEST_ID, req)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rid, p = isotp_recv(vci, timeout_s=max(0.1, deadline - time.time()))
        if not rid or not p:
            continue
        if p[0] == mode + 0x40 and (pid is None or (len(p) > 1 and p[1] == pid)):
            return rid, p
    return None, b""


def decode_dtc(two):
    """Decode a 2-byte DTC to e.g. P0301."""
    sys_ch = "PCBU"[two[0] >> 6]
    return f"{sys_ch}{(two[0] >> 4) & 3}{two[0] & 0xF:X}{two[1] >> 4:X}{two[1] & 0xF:X}"


def cmd_status(vci):
    volts = vci.voltage()
    print(f"battery voltage at OBD pin 16: "
          f"{volts:.2f} V" if volts else "voltage read failed")

    rid, p = obd_request(vci, 0x01, 0x00)
    if rid:
        print(f"supported PIDs 01-20 (from 0x{rid:03X}): {hexs(p[2:])}")
    else:
        print("no response to mode 01 PID 00")

    rid, p = obd_request(vci, 0x01, 0x0C)
    if rid and len(p) >= 4 and p[0] == 0x41:
        rpm = ((p[2] << 8) | p[3]) / 4
        print(f"engine RPM: {rpm:.0f}")
    rid, p = obd_request(vci, 0x01, 0x05)
    if rid and len(p) >= 3 and p[0] == 0x41:
        print(f"coolant temp: {p[2] - 40} degC")

    print("\nVIN (mode 09 PID 02):")
    rid, p = obd_request(vci, 0x09, 0x02, timeout_s=3.0)
    if rid and len(p) >= 3:
        vin = p[3:].decode("ascii", "replace")
        print(f"  {vin}")
    else:
        print("  no response")


def cmd_dtc(vci):
    print("stored DTCs (mode 03):")
    vci.can_write(OBD_REQUEST_ID, bytes([1, 0x03]))
    seen = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        rid, p = isotp_recv(vci, timeout_s=max(0.1, deadline - time.time()))
        if not rid or not p or p[0] != 0x43:
            continue
        seen = True
        count = p[1]
        codes = [decode_dtc(p[2 + i * 2 : 4 + i * 2]) for i in range(count)]
        print(f"  0x{rid:03X}: {count} code(s) {', '.join(codes) if codes else ''}")
    if not seen:
        print("  no responses (or no codes)")
    print("pending DTCs (mode 07):")
    vci.can_write(OBD_REQUEST_ID, bytes([1, 0x07]))
    rid, p = isotp_recv(vci, timeout_s=2.0)
    if rid and p and p[0] == 0x47:
        count = p[1]
        codes = [decode_dtc(p[2 + i * 2 : 4 + i * 2]) for i in range(count)]
        print(f"  0x{rid:03X}: {count} code(s) {', '.join(codes) if codes else ''}")
    else:
        print("  none seen")


def cmd_crank(vci, seconds=20):
    """Log voltage as fast as possible; run while cranking the engine."""
    print(f"logging voltage for {seconds}s — crank the engine now!")
    t0 = time.time()
    vmin, vmax = 99.0, 0.0
    samples = []
    while time.time() - t0 < seconds:
        v = vci.voltage()
        if v is None:
            continue
        samples.append((time.time() - t0, v))
        vmin, vmax = min(vmin, v), max(vmax, v)
        print(f"\r  t={time.time()-t0:5.1f}s  {v:6.2f} V   (min {vmin:.2f} / max {vmax:.2f})",
              end="", flush=True)
    print()
    if samples:
        print(f"samples: {len(samples)}  min: {vmin:.2f} V  max: {vmax:.2f} V")
        dips = [s for s in samples if s[1] < vmax - 0.8]
        if dips:
            print(f"dip detected: lowest {vmin:.2f} V at t={min(dips, key=lambda s: s[1])[0]:.1f}s")
        else:
            print("no significant dip seen — did the starter engage?")


def main():
    ap = argparse.ArgumentParser(description="BavTech HID cable OBD-II tool")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "dtc", "voltage", "sniff", "crank"])
    args = ap.parse_args()

    vci = Vci()
    print(f"cable firmware: {vci.firmware_version()}")

    if args.action == "voltage":
        v = vci.voltage()
        print(f"battery voltage at OBD pin 16: {v:.2f} V" if v else "read failed")
        vci.close()
        return

    if args.action == "crank":
        cmd_crank(vci)
        vci.close()
        return

    vci.can_config()
    if args.action == "sniff":
        try:
            while True:
                cid, d = vci.can_read_frame()
                if cid is not None:
                    print(f"  0x{cid:03X}: {hexs(d)}")
        except KeyboardInterrupt:
            pass
        finally:
            # deliberately no can_stop(): stop/start cycles wedge the cable
            vci.close()
        return

    try:
        if args.action == "status":
            cmd_status(vci)
        elif args.action == "dtc":
            cmd_dtc(vci)
    finally:
        # deliberately no can_stop(): stop/start cycles wedge the cable
        vci.close()


if __name__ == "__main__":
    main()
