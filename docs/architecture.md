# System Architecture

## What this project is

A Bavarian Technic diagnostic cable — sold as a BMW-only tool — repurposed
as a **generic CAN / OBD-II interface** for any modern vehicle. The vendor
software is bypassed entirely: our Python code speaks directly to the
cable's microcontroller over USB HID, and through it to the car.

## The stack

```mermaid
flowchart TB
    subgraph laptop["Laptop (Windows x64 or Armbian aarch64)"]
        app["kia_vci.py<br/>OBD-II modes 01/03/07/09, DTC decode, voltage logger"]
        isotp["ISO-TP layer (ISO 15765-2)<br/>single/multi-frame, flow control"]
        vci["Vci class — VCI protocol<br/>[LEN][CMD][PAYLOAD] framing"]
        hidapi["hidapi (libusb backend on Linux,<br/>native HID on Windows)"]
    end
    subgraph cable["Diag Tech USB Vehicle Interface (VID 1FC9 PID 8084)"]
        mcu["NXP MCU firmware '18-07-24'<br/>command dispatcher + CAN/K-line drivers"]
        can["CAN transceiver"]
        kline["K-line transceiver (unused here)"]
        adc["ADC on OBD pin 16<br/>battery voltage"]
    end
    subgraph car["Vehicle"]
        obd["OBD-II connector<br/>CAN-H pin 6 / CAN-L pin 14 / +12V pin 16"]
        ecm["ECM + other ECUs<br/>respond 0x7E8-0x7EF"]
        bus["500 kbps CAN bus (ISO 15765-4)"]
    end
    app --> isotp --> vci --> hidapi
    hidapi <-->|"64-byte HID reports"| mcu
    mcu --> can & kline & adc
    can <--> obd <--> bus <--> ecm
    adc --- obd
```

Each layer is independently useful:

| Layer | File / component | Replaceable with |
|---|---|---|
| OBD-II application | `kia_vci.py` subcommands | any diagnostic logic |
| ISO-TP transport | `isotp_recv` / `obd_request` | UDS client, manufacturer protocols |
| VCI cable protocol | `Vci` class | — (this is the reverse-engineered part) |
| USB transport | hidapi | any HID library |

## Why the cable works on non-BMW cars

The cable's firmware exposes **raw CAN primitives** (`CAN_CONFIG`,
`CAN_WRITE`, `CAN_READ`) with a 500 kbps preset. BMW's D-CAN and the
legislated OBD-II physical layer (ISO 15765-4) are the *same electrical
thing*: 500 kbps, 11-bit identifiers, on OBD pins 6/14. The only
BMW-specific parts of the vendor product live in its Windows application
layer, which we do not use.

On top of raw CAN, every 2008+ US-market vehicle must implement the same
emissions diagnostics: requests broadcast to CAN ID `0x7DF`, responses
from `0x7E8–0x7EF`, payloads in ISO-TP. That is why one script covers a
2018 Kia Soul, a 2023 Nissan Rogue, and a 2023 Mazda CX-50 without
vehicle-specific code.

## An OBD-II query, end to end

```mermaid
sequenceDiagram
    participant S as kia_vci.py
    participant C as Cable MCU
    participant E as ECM (0x7E8)
    S->>C: HID out: [04][93][0B][DF 07][02 01 0C ...]  (CAN_WRITE to 0x7DF: mode 01 PID 0C)
    C->>E: CAN 0x7DF: 02 01 0C 00 00 00 00 00
    E->>C: CAN 0x7E8: 04 41 0C 1A F8 ...  (RPM = 0x1AF8/4)
    Note over C: frame buffered in RX FIFO
    loop poll until match
        S->>C: HID out: [01][92]  (CAN_READ)
        C->>S: HID in: [0D][92][00][E8 07][04 41 0C 1A F8 ...][5B]
    end
    Note over S: software-filter 0x7E8-0x7EF,<br/>match mode+0x40 and PID
```

Multi-frame responses (e.g. the 17-byte VIN) add ISO-TP flow control: the
ECM sends a First Frame, we must answer `30 00 00` to its physical ID
(response ID − 8) before it sends Consecutive Frames.

## Host environments

- **Windows**: no driver needed; the OS generic HID driver binds
  automatically. (The FTDI driver package shipped by the vendor is for
  older, FTDI-based cable generations — not this hardware.)
- **Linux (tested: Armbian aarch64, ThinkPad X13s)**: kernel `usbhid` +
  `hidraw` are the driver; `python3-hidapi` (libusb backend) does user-space
  access. One udev rule grants non-root access. See README for setup.
- `cable_reset.py` recovers a hung cable remotely via `USBDEVFS_RESET`
  (kernel USB port reset) — see the operational notes in
  [vci-protocol.md](vci-protocol.md#firmware-landmines).
