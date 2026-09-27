"""
Offline simulation for the BavTech OBD-II tool.

Two halves:
  * record_fixture(vci, path, name) — drives a LIVE car with read-only OBD
    requests and captures every response into a JSON fixture.
  * SimVci — a drop-in replacement for the Vci cable class that replays a
    recorded fixture. It re-frames stored responses back into ISO-TP CAN
    frames, so the tool's real obd_request()/isotp_recv()/decoders run
    unchanged against it. No cable, no car, no risk.

This does NOT copy ECU firmware (that needs security-gated memory reads or a
bench flash dump). It captures the car's *behavior at the OBD interface* —
which is what you actually need to develop and test offline.

Usage:
    # capture (live, read-only) — done once per car:
    python3 vci.py record --out fixtures/cx50.json --name "2023 CX-50"
    # develop/test offline against the capture:
    python3 vci.py status --sim fixtures/cx50.json
    python3 vci.py dtc    --sim fixtures/cx50.json
"""

import datetime
import json

from vci import obd_request, OBD_REQUEST_ID


# --------------------------------------------------------------------------
# Recorder (runs against a real Vci; read-only)
# --------------------------------------------------------------------------

def record_fixture(vci, path, name):
    fx = {
        "meta": {
            "name": name,
            "recorded": datetime.datetime.now().isoformat(timespec="seconds"),
            "firmware": vci.firmware_version(),
            "voltage": vci.voltage(),
        },
        "obd": {},
    }

    def cap(mode, pid=None):
        rid, p = obd_request(vci, mode, pid)
        if rid and p:
            key = f"{mode:02X}" + (f":{pid:02X}" if pid is not None else "")
            fx["obd"][key] = {"id": f"{rid:03X}", "resp": p.hex()}
            return p
        return None

    # Sweep every supported-PID bitmap block (0x00,0x20,...) and, for each
    # bit set, capture that PID's live value. All mode 01 = read-only.
    for base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
        p = cap(0x01, base)
        if not p or len(p) < 6:
            break
        bits = int.from_bytes(p[2:6], "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                cap(0x01, base + 1 + i)
        if not (bits & 0x01):   # low bit = "next block supported"
            break

    cap(0x09, 0x02)   # VIN
    cap(0x03)         # stored DTCs
    cap(0x07)         # pending DTCs

    with open(path, "w") as f:
        json.dump(fx, f, indent=2)
    print(f"recorded {len(fx['obd'])} OBD responses from '{name}' -> {path}")
    return fx


# --------------------------------------------------------------------------
# Replay (drop-in for Vci; no hardware)
# --------------------------------------------------------------------------

class SimVci:
    """Replays a recorded fixture. Implements the subset of the Vci API the
    tool actually calls, re-framing stored UDS responses into ISO-TP so the
    real receive/decode path is exercised end to end."""

    def __init__(self, fixture_path):
        with open(fixture_path) as f:
            self.fx = json.load(f)
        self.meta = self.fx.get("meta", {})
        self.responders = self.fx.get("obd", {})
        self._txq = []          # frames waiting to be handed back
        self._pending_cf = None  # [resp_id, remaining_bytes, seq] after a First Frame

    # --- identity -----------------------------------------------------
    def close(self):
        pass

    def firmware_version(self):
        return self.meta.get("firmware", "SIM")

    def voltage(self):
        return self.meta.get("voltage")

    # --- cable control (no-ops in sim) --------------------------------
    def can_config(self, preset=3):
        return (0x90, b"")

    def can_add_filter(self, can_id):
        return (0x91, b"")

    def can_stop(self):
        return (0x94, b"")

    # --- ISO-TP framing of a stored response --------------------------
    def _enqueue_isotp(self, resp_id, payload):
        payload = bytes(payload)
        if len(payload) <= 7:                       # single frame
            frame = bytes([len(payload)]) + payload
            frame += bytes(8 - len(frame))
            self._txq.append((resp_id, frame))
        else:                                       # first frame + wait for FC
            total = len(payload)
            ff = bytes([0x10 | ((total >> 8) & 0x0F), total & 0xFF]) + payload[:6]
            self._txq.append((resp_id, ff))
            self._pending_cf = [resp_id, payload[6:], 1]

    def can_write(self, can_id, data):
        data = bytes(data)
        if can_id == OBD_REQUEST_ID:
            n = data[0]
            req = data[1:1 + n]
            mode = req[0]
            pid = req[1] if len(req) > 1 else None
            key = f"{mode:02X}" + (f":{pid:02X}" if pid is not None else "")
            entry = self.responders.get(key)
            if entry:
                self._enqueue_isotp(int(entry["id"], 16),
                                    bytes.fromhex(entry["resp"]))
            return (0x93, b"")
        # ISO-TP flow control from the tester (0x3x) — release consecutive frames
        if data and (data[0] & 0xF0) == 0x30 and self._pending_cf:
            resp_id, remaining, seq = self._pending_cf
            while remaining:
                chunk = remaining[:7]
                remaining = remaining[7:]
                cf = bytes([0x20 | (seq & 0x0F)]) + chunk
                cf += bytes(8 - len(cf))
                self._txq.append((resp_id, cf))
                seq = (seq + 1) & 0x0F
            self._pending_cf = None
        return (0x93, b"")

    def can_read_frame(self, timeout_ms=100):
        if self._txq:
            return self._txq.pop(0)
        return (None, b"")
