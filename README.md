# codelight — Claude Code status display

<img src="assets/demo.jpg" width="600" alt="GeekMagic Ultra showing the codelight IDLE screen">

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
| Connectivity | 2.4 GHz WiFi only (no Bluetooth), USB - power only, 6-pad debug header |
| Price | 10-15 USD - [GeekMagic Ultra on Aliexpress](https://s.click.aliexpress.com/e/_c32BRoxx) (affiliate link) |

---

## Building

Pre-built firmware binaries are available on the
[Releases page](https://github.com/henrikekblad/codelight/releases) — download
`codelight-vX.Y.Z.bin` and skip straight to [Flashing](#flashing).

To build from source, install [PlatformIO](https://platformio.org/), then:

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

The Python script `companion/codelight.py` runs on your computer and instantly pushes
status data to the device.

### Dependencies

**Arch Linux**
```bash
sudo pacman -S python-requests
```

**Debian / Ubuntu**
```bash
sudo apt install python3-requests
```

### Run

```bash
python3 companion/codelight.py --device claude-screen.local
```

On first run the script automatically installs Claude Code hooks in
`~/.claude/settings.json` so it can track working/waiting state in real time.

Use `--dry-run` to print the payload without posting, and `--verbose` to see
raw usage captures and session scans.

### Run as a systemd user service

Create `~/.config/systemd/user/codelight.service`:

```ini
[Unit]
Description=Claude Code status monitor

[Service]
ExecStart=/usr/bin/python3 -u /path/to/companion/codelight.py \
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
systemctl --user enable --now codelight
systemctl --user status codelight   # verify it's running

# To start at boot without being logged in:
sudo loginctl enable-linger $USER
```

Useful commands:

```bash
journalctl --user -fu codelight     # live logs
systemctl --user restart codelight  # restart after config change
systemctl --user disable --now codelight  # disable
```

### Optional: shared secret

If multiple people are on the same network, set a secret in the device config
page and pass it to the script:

```bash
python3 companion/codelight.py --device claude-screen.local --secret mypassword
```

---

## How it works

```
Claude Code               codelight.py (daemon)          GeekMagic Ultra
───────────────           ─────────────────────          ───────────────
                          Unix socket thread
hooks fire on  ────────►  receives event        ───────►  POST /status
tool use /      --hook    updates state                    immediately
messages        mode      POSTs to device

                          Usage poller thread
                          fetches claude.ai API  ───────►  POST /status
                          every 60 s                        after each poll
```

`codelight.py` runs as a persistent daemon with two threads. Status updates
reach the display the moment a hook fires — there is no polling delay.

### Status detection — hooks

Claude Code hooks are shell commands that Claude Code invokes at specific points
during a session. On first run, `codelight.py` registers entries in
`~/.claude/settings.json` for events such as `PreToolUse`, `PostToolUse`,
`PermissionRequest`, and `SessionEnd`. When an event fires, Claude Code runs:

```
python3 codelight.py --hook working
```

with session metadata on stdin. The hook mode connects to a Unix socket at
`~/.claude/codelight.sock`, sends a one-line JSON event, and exits in ~1 ms.
The daemon's socket thread receives the event, updates its in-memory session
state, and immediately POSTs to the device. If the daemon is not running the
hook falls back to writing a state file so no errors appear in the terminal.

### Usage data — claude.ai API

Every 60 seconds the usage thread fetches `https://claude.ai/api/oauth/usage`
using the OAuth access token from `~/.claude/.credentials.json` — the same
credential Claude Code itself uses, so no extra authentication is needed. The
response contains:

- `five_hour.utilization` — current 5-hour session window (0–100 %)
- `seven_day.utilization` — rolling 7-day total (0–100 %)
- `resets_at` — ISO-8601 timestamp for each window reset

Values are cached so the display always shows something even when the API is
temporarily unreachable.

### Display update — POST /status

The daemon POSTs a JSON payload to `http://<device>/status` on two triggers:
every hook event (immediate) and after each usage poll (every 60 s). The
ESP8266 re-renders the full display on each received payload.

---

## Uninstalling

1. **Stop the monitor** — Ctrl-C, or if running as a service:
   ```bash
   systemctl --user disable --now codelight
   ```

2. **Remove hooks and state files:**
   ```bash
   python3 companion/codelight.py --uninstall
   ```
   This removes all codelight entries from `~/.claude/settings.json` and deletes
   `~/.claude/codelight.sock` and `~/.claude/monitor_state/`.

   > **Stop the daemon before uninstalling.** If it is still running it will
   > re-install the hooks on its next startup.

---

## Multiple screens on one network

Each device must have a unique name. Set them via the config page before
connecting to the shared network. Each person runs their own companion script
pointing to their own device — no other coordination needed.
