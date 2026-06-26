# claude_monitor — install guide

Pushes your Claude Code usage stats to a GeekMagic Ultra screen on the local WiFi.

---

## 1. Give your screen a unique name

Each screen on the network **must have a different hostname** or they will
clash in mDNS. Do this before anything else:

1. Connect to the screen's config page in a browser.
   - First-time setup: connect to the WiFi AP `claude-screen-setup` →
     open `http://192.168.4.1`
   - Already on WiFi: open `http://claude-screen.local`
2. Set **Device Name** to something unique, e.g. `claude-screen` or
   `alice-desk`.  Only letters, digits, and hyphens are allowed.
3. Save.  The screen reboots and is now reachable at
   `http://claude-screen.local`.

---

## 2. Install dependencies

### Arch Linux
```bash
sudo pacman -S python-requests tmux
```

### Debian / Ubuntu
```bash
sudo apt install python3-requests tmux
```

### Other (pip)
```bash
pip install requests   # tmux must be installed separately via your package manager
```

---

## 3. Test it manually

```bash
python3 -u claude_monitor.py --device claude-screen.local --dry-run --verbose
```

`--dry-run` prints the payload instead of posting to the screen.
`--verbose` shows the raw tmux capture and session scan.

On first run the script auto-installs Claude Code hooks in `~/.claude/settings.json`
so it can track working/waiting state in real time. No manual hook setup needed.

Once it looks correct, drop `--dry-run`:

```bash
python3 -u claude_monitor.py --device claude-screen.local
```

---

## 4. Run as a systemd user service

A **user service** (no root) is the right choice — the script needs access
to `~/.claude/` and should follow your login session.

### 4a. Create the service file

```bash
mkdir -p ~/.config/systemd/user
```

Create `~/.config/systemd/user/claude-monitor.service`:

```ini
[Unit]
Description=Claude Code status monitor
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 -u /path/to/companion/claude_monitor.py \
    --device claude-screen.local
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

Replace `/path/to/companion/claude_monitor.py` and `claude-screen.local`
with your actual path and screen name.

If your screen has a shared secret configured, add `--secret yourpassword`
to the `ExecStart` line.

### 4b. Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-monitor
systemctl --user status claude-monitor   # verify it's running
```

### 4c. Auto-start on boot (without logging in)

By default user services only run while you are logged in.  To start them
at boot:

```bash
sudo loginctl enable-linger $USER
```

This is a one-time command per machine.

---

## 5. Multiple screens on the same network

Each person runs their own service pointing to their own screen:

| Person | Device Name in config | Script flag              |
|--------|----------------------|--------------------------|
| claude | `claude-screen`      | `--device claude-screen.local` |
| Alice  | `alice-screen`       | `--device alice-screen.local`  |
| Bob    | `bob-screen`         | `--device bob-screen.local`    |

No other coordination needed — each screen is independent.

---

## 6. Useful commands

```bash
# View live logs
journalctl --user -fu claude-monitor

# Restart after config change
systemctl --user restart claude-monitor

# Disable
systemctl --user disable --now claude-monitor
```
