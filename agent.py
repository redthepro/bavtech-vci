#!/usr/bin/env python3
"""
Read-only OBD-II HTTP agent for the BavTech cable.

Runs on the machine physically holding the cable (Armbian aarch64 or
Windows) and serves JSON so a remote GUI can operate it over the network.

SAFETY: read-only. It reuses the audited kia_vci.Vci driver, whose only
bus transmissions are OBD modes 01/03/07/09 + ISO-TP flow control. There
is no endpoint that writes, clears, actuates, or programs anything.

The cable is opened ONCE and CAN is configured ONCE for the process
lifetime (repeated stop/config wedges the firmware). A lock serializes
cable access across HTTP threads.

Endpoints (all GET):
    /health              -> {mode, firmware, voltage}
    /status              -> {voltage, rpm, coolant_c, speed_kph, vin, ...}
    /dtc                 -> {stored:[...], pending:[...]}
    /pid/<mode>/<pid>    -> {responder, raw}      e.g. /pid/01/0C
    /vin                 -> {vin}
Auth: send header  X-Token: <token>  (default "cx50", or $AGENT_TOKEN).
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import kia_vci
from kia_vci import Vci, obd_request, isotp_recv, decode_dtc

TOKEN = os.environ.get("AGENT_TOKEN", "cx50")
PORT = int(os.environ.get("AGENT_PORT", "8177"))


class Diag:
    def __init__(self, sim=None):
        self.lock = threading.Lock()
        if sim:
            from sim import SimVci
            self.vci = SimVci(sim)
            self.mode = f"sim:{os.path.basename(sim)}"
        else:
            self.vci = Vci()
            self.vci.can_config()
            self.mode = "live"
        self.firmware = self.vci.firmware_version()

    def health(self):
        with self.lock:
            return {"mode": self.mode, "firmware": self.firmware,
                    "voltage": self.vci.voltage()}

    def _pid_val(self, mode, pid, timeout_s=2.0):
        rid, p = obd_request(self.vci, mode, pid, timeout_s=timeout_s)
        return rid, p

    def status(self):
        with self.lock:
            out = {"voltage": self.vci.voltage()}
            rid, p = self._pid_val(0x01, 0x00)
            out["responder"] = f"{rid:03X}" if rid else None
            out["supported_01_20"] = p[2:].hex() if p else None

            rid, p = self._pid_val(0x01, 0x0C)
            out["rpm"] = round(((p[2] << 8) | p[3]) / 4, 1) if (p and len(p) >= 4 and p[0] == 0x41) else None
            rid, p = self._pid_val(0x01, 0x05)
            out["coolant_c"] = (p[2] - 40) if (p and len(p) >= 3 and p[0] == 0x41) else None
            rid, p = self._pid_val(0x01, 0x0D)
            out["speed_kph"] = p[2] if (p and len(p) >= 3 and p[0] == 0x41) else None
            rid, p = self._pid_val(0x01, 0x11)
            out["throttle_pct"] = round(p[2] * 100 / 255, 1) if (p and len(p) >= 3 and p[0] == 0x41) else None
            rid, p = self._pid_val(0x01, 0x2F)
            out["fuel_pct"] = round(p[2] * 100 / 255, 1) if (p and len(p) >= 3 and p[0] == 0x41) else None
            rid, p = self._pid_val(0x09, 0x02, timeout_s=3.0)
            out["vin"] = p[3:].decode("ascii", "replace") if (p and len(p) >= 3) else None
            return out

    def pid(self, mode, pid):
        with self.lock:
            rid, p = obd_request(self.vci, mode, pid)
            return {"mode": mode, "pid": pid,
                    "responder": f"{rid:03X}" if rid else None,
                    "raw": p.hex() if p else None}

    def dtc(self):
        with self.lock:
            result = {"stored": [], "pending": []}
            # mode 03 stored
            self.vci.can_write(0x7DF, bytes([1, 0x03]))
            import time
            deadline = time.time() + 2.0
            while time.time() < deadline:
                rid, p = isotp_recv(self.vci, timeout_s=max(0.1, deadline - time.time()))
                if not rid or not p or p[0] != 0x43:
                    continue
                count = p[1]
                for i in range(count):
                    two = p[2 + i * 2:4 + i * 2]
                    if len(two) == 2 and any(two):
                        result["stored"].append({"code": decode_dtc(two), "ecu": f"{rid:03X}"})
            # mode 07 pending
            self.vci.can_write(0x7DF, bytes([1, 0x07]))
            rid, p = isotp_recv(self.vci, timeout_s=2.0)
            if rid and p and p[0] == 0x47:
                count = p[1]
                for i in range(count):
                    two = p[2 + i * 2:4 + i * 2]
                    if len(two) == 2 and any(two):
                        result["pending"].append({"code": decode_dtc(two), "ecu": f"{rid:03X}"})
            return result


class Handler(BaseHTTPRequestHandler):
    diag = None  # set at startup

    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "dashboard.html"), "rb") as f:
                body = f.read()
        except OSError:
            body = b"<h1>dashboard.html missing</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        page = urlparse(self.path).path
        if page in ("/", "/index.html", "/dashboard"):
            return self._send_html()   # UI page needs no token; its API calls do
        if self.headers.get("X-Token") != TOKEN:
            return self._send(401, {"error": "bad or missing X-Token"})
        path = urlparse(self.path).path.strip("/").split("/")
        try:
            if path == [""] or path[0] == "health":
                return self._send(200, self.diag.health())
            if path[0] == "status":
                return self._send(200, self.diag.status())
            if path[0] == "dtc":
                return self._send(200, self.diag.dtc())
            if path[0] == "vin":
                return self._send(200, {"vin": self.diag.status().get("vin")})
            if path[0] == "pid" and len(path) == 3:
                return self._send(200, self.diag.pid(int(path[1], 16), int(path[2], 16)))
        except Exception as e:
            return self._send(500, {"error": repr(e)})
        return self._send(404, {"error": "unknown endpoint"})


def main():
    sim = None
    if "--sim" in sys.argv:
        sim = sys.argv[sys.argv.index("--sim") + 1]
    Handler.diag = Diag(sim=sim)
    print(f"agent: mode={Handler.diag.mode} firmware={Handler.diag.firmware} "
          f"listening on :{PORT} (token required)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
