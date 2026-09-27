# bavtech-vci

Using a **Bavarian Technic** (BMW-specific) diagnostic cable as a generic
CAN / OBD-II interface for any modern vehicle — tested on a 2018 Kia Soul,
next up a 2023 Nissan Rogue SV and 2023 Mazda CX-50.

## Background

The Bavarian Technic cable is sold as a BMW-only tool, but the hardware is
a generic multi-protocol vehicle interface: an NXP microcontroller with
K-line and CAN transceivers that enumerates as a **USB HID device**
(VID `0x1FC9`, PID `0x8084`, product string "Diag Tech USB Vehicle
Interface"). No kernel driver needed — libusb/hidapi talks to it directly
on Windows, Linux, or macOS.

The MCU firmware exposes a command set that includes a raw CAN layer
(configure bitrate, send/receive arbitrary 11-bit frames). The vendor's
BMW software is just one client of that firmware; nothing in the cable is
BMW-locked. The protocol below was reverse-engineered from `VCI4.dll`
(an unobfuscated .NET assembly shipped with the vendor software) for
interoperability with our own cable and vehicles, then verified live.

## Wire protocol

Transport: 64-byte HID reports, report ID 0. Every message, both
directions, is one frame:

```
[LEN][CMD][PAYLOAD ...]      LEN = len(PAYLOAD) + 1
```

Useful firmware commands (`Vci4.VCICommand`):

| cmd  | name         | notes                                             |
|------|--------------|---------------------------------------------------|
| 0x02 | FIRMWARE_VER | returns ASCII date string                         |
| 0x03 | SERIAL_NUM   | cable serial blob                                 |
| 0x0A | ADC          | returns `[status][mV_lo][mV_hi]` — battery voltage at OBD pin 16 |
| 0x90 | CAN_CONFIG   | payload `[preset]`; preset **3** = 500 kbps       |
| 0x92 | CAN_READ     | pops one frame from the cable's RX FIFO           |
| 0x93 | CAN_WRITE    | payload `[len+3][id_lo][id_hi][data x8]`          |

Received CAN frames come back as:

```
[status][id_lo][id_hi][data x8][extra][0x5B]
```

### Firmware landmines (learned the hard way)

- **Never use `CAN_ADD_FILTER` (0x91)** — it latches one stale frame and
  the read path returns it forever.
- **Never cycle `CAN_STOP`/`CAN_CONFIG` repeatedly** — it wedges the CAN
  transmitter *and* freezes the ADC until the cable is hard power-cycled
  (unplug both USB and OBD ends). Configure once per session.
- `CAN_READ_ALL` (0x95) returns a 4-byte status, not a frame dump.
- The RX FIFO buffers all bus traffic; on a busy bus, poll fast and filter
  in software, and expect responses to be behind a backlog.

## The tool: `kia_vci.py`

Despite the name it is vehicle-agnostic — it speaks standardized OBD-II
(ISO 15765-4 at 500 kbps, 11-bit addressing), which every 2008+ US-market
vehicle implements. Requires Python 3 and the `hidapi` package.

```
python kia_vci.py status    # battery voltage, supported PIDs, coolant, VIN
python kia_vci.py dtc       # stored (mode 03) + pending (mode 07) trouble codes
python kia_vci.py voltage   # one-shot battery voltage from OBD pin 16
python kia_vci.py crank     # 20 s voltage logger @ ~64 Hz — no-start diagnosis
python kia_vci.py sniff     # dump raw CAN frames from the bus
```

What's inside:

- `Vci` class — HID transport + the firmware command wrappers above.
- `isotp_recv` / `obd_request` — a minimal ISO-TP (ISO 15765-2) layer:
  single-frame and multi-frame reassembly with flow control, and
  request/response matching against the OBD broadcast ID `0x7DF` and
  responder IDs `0x7E8–0x7EF`.
- Decoders for a few mode-01 PIDs, mode-03/07 DTC codes, mode-09 VIN.

The `debug_*.py` scripts are the scratch probes used while reverse
engineering; kept for reference.

## Field results (2018 Kia Soul)

Read supported-PID bitmap and coolant temp, scanned DTCs, and diagnosed a
no-start: the voltage logger showed a starter "click" with **zero** battery
sag (flat 11.5 V), proving the starter's high-current path never conducted.
Root cause found minutes later: sheared/corroded B-terminal stud on the
starter solenoid.

## Known gaps

- Mode-09 VIN (multi-frame) is unreliable — our ISO-TP flow-control frame
  can arrive late because HID polling caps at a few hundred frames/s.
- 11-bit addressing only (`CAN_WRITE` takes a 16-bit ID field).
- K-line commands (0xB0/0xB1) exist in firmware but are unexplored.

## Linux setup (tested on Armbian/aarch64, ThinkPad X13s)

```sh
sudo apt install python3-hidapi   # libusb-backend build; works as-is
sudo tee /etc/udev/rules.d/99-bavtech-vci.rules << "RULES"
SUBSYSTEM=="usb", ATTR{idVendor}=="1fc9", ATTR{idProduct}=="8084", MODE="0666", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1fc9", ATTRS{idProduct}=="8084", MODE="0666", TAG+="uaccess"
RULES
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The Debian/Ubuntu `python3-hidapi` package links the **libusb** backend of
hidapi, so the `usb`-subsystem rule is the one that matters; the hidraw
rule is kept for hidraw-backend builds. No kernel driver or vendor
software needed on Linux at all.

## Documentation

- [docs/architecture.md](docs/architecture.md) — system stack, why a BMW cable works on any car, query walkthrough
- [docs/vci-protocol.md](docs/vci-protocol.md) — full protocol spec: framing, command reference, RX format, firmware landmines
- [docs/reverse-engineering.md](docs/reverse-engineering.md) — methodology: how the protocol was extracted and verified

## Offline simulation (develop without the car)

You cannot copy an ECU's firmware over OBD (that needs security-gated
memory reads or a bench flash dump). But you can capture the car's
*behavior at the OBD interface* and replay it offline — which is what you
actually need for development.

```sh
# 1. capture once, live, READ-ONLY (car at full ignition):
python3 kia_vci.py record --out fixtures/cx50.json --name "2023 CX-50"

# 2. then develop/test offline — no cable, no car, no risk:
python3 kia_vci.py status --sim fixtures/cx50.json
python3 kia_vci.py dtc    --sim fixtures/cx50.json
```

`record` sweeps every supported mode-01 PID plus DTCs and VIN and stores the
responses in a JSON fixture. `--sim` swaps the cable for `SimVci` (see
`sim.py`), which re-frames those responses back into ISO-TP so the tool's
real receive/decode path runs unchanged. Fixtures contain the vehicle VIN;
scrub it if you share one.
