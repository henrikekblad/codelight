# codelight — screen firmware

<img src="../assets/demo.jpg" width="600" alt="GeekMagic Ultra showing the codelight IDLE screen">

Custom ESP8266 firmware for the **GeekMagic Ultra** that turns it into a live
Claude Code dashboard. The screen connects outbound to the companion daemon as a
WebSocket client, discovers it automatically via mDNS, and re-renders on every
push.

> **Work in progress — not yet fully verified on real hardware.** The firmware compiles
> and the companion script runs, but the full stack has not been tested on an actual
> GeekMagic Ultra. I accidentally ripped the screen flex cable while testing/flashing.
> Still waiting for new screens to arrive from Aliexpress for final test.

## Hardware

| | |
|---|---|
| Chip | ESP8266 (80 MHz, ~45 KB RAM) |
| Display | ST7789V 240×240 IPS TFT |
| Flash | 4 MB |
| Connectivity | 2.4 GHz WiFi only (no Bluetooth), USB — power only, 6-pad debug header |
| Price | 10–15 USD — [GeekMagic Ultra on Aliexpress](https://s.click.aliexpress.com/e/_c32BRoxx) (affiliate link) |

## Building

Pre-built firmware binaries are on the
[Releases page](https://github.com/henrikekblad/codelight/releases) — download
`codelight-vX.Y.Z.bin` and skip straight to [Flashing](#flashing).

To build from source, install [PlatformIO](https://platformio.org/), then from
this directory:

```bash
pio run
```

The firmware binary ends up at `.pio/build/geekmagic_ultra/firmware.bin`.

## Flashing

### Option A — Stock firmware OTA (easiest, no disassembly)

The GeekMagic Ultra ships with a stock firmware that exposes a web OTA page.

1. Power on the device. It creates a WiFi AP — connect to it from your computer.
2. Open the stock firmware's update page in a browser (usually `http://192.168.4.1/update`
   or check the screen for the address).
3. Upload `.pio/build/geekmagic_ultra/firmware.bin`.
4. The device reboots into the new firmware and shows a setup screen.

### Option B — FTDI serial adapter (if stock OTA isn't available)

The device has a 6-pad debug header. With a 3.3 V FTDI adapter you can flash
directly over serial without any tools.

> **Use 3.3 V logic only. 5 V will damage the ESP8266.**

#### Pad layout

```
⬜ 1  GND
⚪ 2  TXD0   ← connect to FTDI RX
⚪ 3  RXD0   ← connect to FTDI TX
⚪ 4  3V3    ← connect to FTDI 3.3 V out
⚪ 5  GPIO0  ← pull LOW to enter bootloader
⚪ 6  RST
```

#### Wiring

| Device pad | FTDI pin |
|---|---|
| 1 GND | GND (black) | 
| 2 TXD0 | RX (green) |
| 3 RXD0 | TX (white) |
| 4 3V3 | 3V3 |
| 5 GPIO0 | GND (via jumper wire) |

Note that **TX/RX are crossed**: the device's transmit (TXD0) connects to the
adapter's receive (RX), and vice versa.

#### Enter bootloader mode

ESP8266 enters its serial bootloader when GPIO0 is held LOW at reset:

1. Connect GPIO0 (pad 5) to GND (pad 1) with a jumper wire.
2. Power the device through the FTDI adapter (connect 3V3 and GND last).
   — or briefly bridge RST (pad 6) to GND if it's already powered.
3. The screen stays dark — the ESP8266 is now waiting for a flash command.

#### Flash

```bash
# Install esptool if needed
python3 -m pip install esptool

python3 -m esptool --port /dev/ttyUSB0 --baud 921600 \
    write_flash 0x0 .pio/build/geekmagic_ultra/firmware.bin
```

Replace `/dev/ttyUSB0` with your actual serial port (`/dev/ttyACM0`, `COM3`, etc.).

4. Remove the GPIO0–GND jumper.
5. Reset the device (bridge RST to GND briefly, or power-cycle).

### Option C — OTA after first flash

Once the custom firmware is running, subsequent updates go through the built-in
ElegantOTA page at `http://<device>.local/update`. No cables needed.

## First-time setup

On first boot (or when no configured network is reachable) the device starts in AP
mode and shows setup instructions on screen:

1. Connect your computer to the WiFi AP **`claude-screen-setup`**.
2. Open `http://192.168.4.1` in a browser.
3. Enter your WiFi credentials (up to 3 networks, tried in order on every boot).
4. Set a **device name** — it becomes the mDNS hostname, e.g. `my-screen`
   → reachable as `http://my-screen.local` for config and OTA.
5. Set a **companion name** — the `--name` value of the `codelight.py` daemon you
   want to connect to (e.g. `henrik-laptop`). Leave blank to connect to the first
   companion found on the network.
6. Optionally set a **companion host** — the IP address of the machine running
   `codelight.py` (e.g. `192.168.1.100`). When set, mDNS discovery is skipped and
   the screen connects directly. Useful if mDNS is unreliable on your network.
7. Optionally set a **companion secret** to match `--secret` on the daemon.
8. Click **Save & apply**. The device reboots, connects to your network, and
   discovers the companion automatically via mDNS (or directly if a host is set).

Config is stored in LittleFS and survives firmware OTA updates.

## How it connects

On boot the screen connects to the companion daemon over WebSocket:

- **Direct IP** (if *companion host* is set): connects immediately without mDNS.
- **mDNS discovery** (default): queries for `_codelight._tcp` services, filtered by
  companion name if configured.

Once connected it receives push updates in real time. If the connection drops it
reconnects automatically after 15 seconds. The timezone offset is pushed by the
companion on every new connection so the clock always shows the correct local time.

## Debug page

Every device running the custom firmware exposes a live debug log at
`http://<device>.local/debug`. It shows timestamped internal events (WiFi,
WebSocket, incoming status) and includes a live screenshot of the current display
state in the top-right corner, updated every second.

## Multiple screens or companions on one network

- **Multiple screens**: each device needs a unique device name set in its config
  page. They are independent — each connects to whichever companion it is
  configured to find.
- **Multiple companions** (e.g. in an office): set the **companion name** in each
  screen's config page to the `--name` of its intended companion. Screens ignore
  companions with non-matching names.
