# codelight companion

The Python daemon `codelight.py` runs on your computer and pushes Claude Code
status to all connected clients — the GeekMagic Ultra screen, Android widget,
and GNOME extension — over a single WebSocket server.

## Dependencies

**Arch Linux**
```bash
sudo pacman -S python-websockets python-zeroconf
```

**Debian / Ubuntu**
```bash
sudo apt install python3-websockets
pip install zeroconf          # or: sudo apt install python3-zeroconf
```

`websockets` powers the WebSocket server that all clients connect to.
`zeroconf` advertises the daemon via mDNS so clients discover it automatically —
both are required.

## Run

```bash
python3 companion/codelight.py --name henrik-laptop
```

`--name` is required. It is the mDNS service instance name clients use to find
this daemon on the network. Use something unique per machine (e.g.
`henrik-laptop`, `alice-workstation`).

**With a shared secret** (recommended in shared networks):
```bash
python3 companion/codelight.py --name henrik-laptop --secret mypassword
```

Set the same secret in the screen's config page and in the Android app.

**Dry run** — print payload to terminal, no broadcast:
```bash
python3 companion/codelight.py --name henrik-laptop --dry-run
```

On first run the script automatically installs Claude Code hooks in
`~/.claude/settings.json` so it can track working/waiting state in real time.

Use `--verbose` to see raw socket events and usage API responses.

## Run as a systemd user service

Create `~/.config/systemd/user/codelight.service`:

```ini
[Unit]
Description=Claude Code status monitor

[Service]
ExecStart=/usr/bin/python3 -u /path/to/companion/codelight.py \
    --name henrik-laptop
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

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

## Multiple companions on the same network

Each person runs their own daemon with a distinct `--name`:

```bash
# Henrik's laptop
python3 codelight.py --name henrik-laptop

# Alice's laptop
python3 codelight.py --name alice-laptop
```

Clients (screen, Android) are configured with the companion name of the person
they belong to and ignore the others. See the screen's config page for the
**Companion name** field.

## Firewall

The daemon needs two ports reachable from clients on your network:

| Port | Protocol | Purpose |
|------|----------|---------|
| 5353 | UDP | mDNS — lets clients discover the daemon automatically |
| 8765 | TCP | WebSocket — the actual data connection |

**ufw:**
```bash
sudo ufw allow 8765/tcp comment "codelight WebSocket"
sudo ufw allow 5353/udp comment "codelight mDNS"
```

**firewalld:**
```bash
sudo firewall-cmd --add-port=8765/tcp --permanent
sudo firewall-cmd --add-port=5353/udp --permanent
sudo firewall-cmd --reload
```

## Uninstalling

1. Stop the daemon (Ctrl-C, or `systemctl --user disable --now codelight`).
2. Remove hooks and state files:
   ```bash
   python3 companion/codelight.py --uninstall
   ```
   This removes all codelight entries from `~/.claude/settings.json` and deletes
   `~/.claude/codelight.sock` and `~/.claude/monitor_state/`.

> **Stop the daemon before uninstalling.** If it is still running it will
> re-install the hooks on its next startup.

## How it works

```
Claude Code               codelight.py (daemon)
───────────────           ─────────────────────
                          Unix socket thread
hooks fire on  ────────►  receives event         broadcast
tool use /      --hook    updates state          ────────►  GeekMagic Ultra (WS client)
messages        mode                             ────────►  Android widget  (WS client)
                                                 ────────►  GNOME extension (WS client)
                          Usage poller thread
                          fetches claude.ai API   push on
                          every 60 s              each poll

                          WebSocket server (:8765)
                          clients connect in ◄───  screen discovers daemon via mDNS
                                             ◄───  Android discovers daemon via mDNS

                          mDNS advertisement
                          _codelight._tcp
```

Status updates reach clients the moment a Claude Code hook fires — there is no
polling delay on the client side.

### Status detection — hooks

Claude Code hooks are shell commands invoked at specific points during a session.
On first run, `codelight.py` registers entries in `~/.claude/settings.json` for
events such as `PreToolUse`, `PostToolUse`, `PermissionRequest`, and `SessionEnd`.
When an event fires, Claude Code runs:

```
python3 codelight.py --hook working
```

with session metadata on stdin. The hook mode connects to a Unix socket at
`~/.claude/codelight.sock`, sends a one-line JSON event, and exits in ~1 ms.
The daemon's socket thread receives the event, updates its in-memory session
state, and immediately broadcasts to all connected WebSocket clients. If the
daemon is not running the hook falls back to writing a state file so no errors
appear in the terminal.

### Usage data — claude.ai API

Every 60 seconds the usage thread fetches `https://claude.ai/api/oauth/usage`
using the OAuth access token from `~/.claude/.credentials.json` — the same
credential Claude Code itself uses, so no extra authentication is needed. The
response contains:

- `five_hour.utilization` — current 5-hour session window (0–100 %)
- `seven_day.utilization` — rolling 7-day total (0–100 %)
- `resets_at` — ISO-8601 timestamp for each window reset

Values are cached so clients always show something even when the API is
temporarily unreachable.
