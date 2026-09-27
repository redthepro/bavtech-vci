"""Probe the Diag Tech (Bavarian Technic) HID vehicle interface.

Enumerates the device, prints its strings and report sizes, then tries a
harmless FIRMWARE_VER (0x02) VCI command with a few candidate framings.
"""
import time

import hid

VID, PID = 0x1FC9, 0x8084


def main():
    infos = [d for d in hid.enumerate(VID, PID)]
    if not infos:
        print("device not found")
        return
    for d in infos:
        print({k: d[k] for k in ("path", "manufacturer_string", "product_string",
                                 "serial_number", "usage_page", "usage",
                                 "interface_number")})

    dev = hid.device()
    dev.open(VID, PID)
    print("\nopened:", dev.get_manufacturer_string(), "/", dev.get_product_string(),
          "/ sn:", dev.get_serial_number_string())
    dev.set_nonblocking(0)

    # VCI frame: [LEN][CMD] with LEN = payload+1; FIRMWARE_VER = 0x02
    vci = bytes([0x01, 0x02])

    candidates = {
        "reportid0 + frame, padded to 64": bytes([0x00]) + vci + bytes(64 - len(vci)),
        "reportid0 + frame, unpadded": bytes([0x00]) + vci,
        "frame only, padded to 64": vci + bytes(64 - len(vci)),
    }
    for name, out in candidates.items():
        print(f"\n--- write ({name}): {out[:8].hex(' ')} ... len={len(out)}")
        try:
            n = dev.write(list(out))
            print(f"    wrote {n} bytes")
        except OSError as e:
            print(f"    write failed: {e}")
            continue
        deadline = time.time() + 1.0
        got = False
        while time.time() < deadline:
            data = dev.read(64, timeout_ms=200)
            if data:
                got = True
                print(f"    read {len(data)}: {bytes(data).hex(' ')}")
        if got:
            print("    ^^^ RESPONSE — this framing works")
            break
        print("    no response")
    dev.close()


if __name__ == "__main__":
    main()
