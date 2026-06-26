# codelight — Claude Code status display

> **Work in progress — not yet fully verified on real hardware.** The firmware compiles
> and the companion script runs, but the full stack has not been tested on an actual
> GeekMagic Ultra. I accidently ripped the screen flex cable while testing/flashing. Still waiting for new screens to arrive from Aliexpress for final test.

Custom firmware for the **GeekMagic Ultra** that turns it into a live Claude Code
dashboard. A companion Python script on your computer polls usage and session state
and pushes it to the device over WiFi.

<table border="1" padding="3"><tr>
<td align="center"><img src="assets/screen-working.svg" width="160"></td>
<td align="center"><img src="assets/screen-waiting.svg" width="160"></td>
<td align="center"><img src="assets/screen-idle.svg" width="160"></td>
</tr></table>


## Hardware

| | |
|---|---|
| Chip | ESP8266 (80 MHz, ~45 KB RAM) |
| Display | ST7789V 240×240 IPS TFT |
| Flash | 4 MB |
| Connectivity | 2.4 GHz WiFi only (no Bluetooth) |

[GeekMagic Ultra on Aliexpress](https://s.click.aliexpress.com/e/_c32BRoxx) (affiliate link)

---

## Building

Install [PlatformIO](https://platformio.org/), then:

```bash
pio run
```

The firmware binary ends up at `.pio/build/geekmagic_ultra/firmware.bin`.

---

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
| 1 GND | GND |
| 2 TXD0 | RX |
| 3 RXD0 | TX |
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

---

## First-time WiFi setup

On first boot (or when no configured network is reachable) the device starts in AP
mode and shows setup instructions on screen:

1. Connect your computer to the WiFi AP **`claude-screen-setup`**.
2. Open `http://192.168.4.1` in a browser.
3. Enter your WiFi credentials (up to 3 networks, tried in order on every boot).
4. Set a **device name** — it becomes the mDNS hostname, e.g. `my-screen`
   → reachable as `http://my-screen.local`.
5. Click **Save & apply**. The device reboots and connects to your network.

Config is stored in LittleFS and survives firmware OTA updates.

---

## Companion script

The Python script `companion/claude_monitor.py` runs on your computer and pushes
status data to the device every 2 seconds.

### Dependencies

**Arch Linux**
```bash
sudo pacman -S python-requests tmux
```

**Debian / Ubuntu**
```bash
sudo apt install python3-requests tmux
```

### Run

```bash
python3 companion/claude_monitor.py --device claude-screen.local
```

On first run the script automatically installs Claude Code hooks in
`~/.claude/settings.json` so it can track working/waiting state in real time.

Use `--dry-run` to print the payload without posting, and `--verbose` to see
raw usage captures and session scans.

### Run as a systemd user service

Create `~/.config/systemd/user/claude-monitor.service`:

```ini
[Unit]
Description=Claude Code status monitor

[Service]
ExecStart=/usr/bin/python3 -u /path/to/companion/claude_monitor.py \
    --device claude-screen.local
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

`network-online.target` is a system target not available in user units — omit it.
The script retries failed POSTs automatically, so it handles startup races without
ordering constraints.

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-monitor
systemctl --user status claude-monitor   # verify it's running

# To start at boot without being logged in:
sudo loginctl enable-linger $USER
```

Useful commands:

```bash
journalctl --user -fu claude-monitor     # live logs
systemctl --user restart claude-monitor  # restart after config change
systemctl --user disable --now claude-monitor  # disable
```

### Optional: shared secret

If multiple people are on the same network, set a secret in the device config
page and pass it to the script:

```bash
python3 companion/claude_monitor.py --device claude-screen.local --secret mypassword
```

---

## Multiple screens on one network

Each device must have a unique name. Set them via the config page before
connecting to the shared network. Each person runs their own companion script
pointing to their own device — no other coordination needed.
