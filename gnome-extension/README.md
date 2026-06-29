# codelight — GNOME Shell extension

A GNOME Shell extension that shows Claude Code status in the top bar.

<img src="../assets/gnome-extension.png" width="600" alt="codelight GNOME Shell extension">

The panel indicator shows **WORKING** (orange), **WAITING** (red), or **IDLE** (green).
Click it to see a popup with session and weekly token usage bars, number of active
sessions, and a Settings link. The extension connects to the companion daemon via
WebSocket and reconnects automatically — updates are instant, not polled.

Requires **GNOME 45 or later** and the companion daemon from
[companion/README.md](../companion/README.md).

## Install

```bash
cd gnome-extension
bash install.sh
```

The script copies files to `~/.local/share/gnome-shell/extensions/codelight@sensnology.se/`,
compiles the GSettings schema, and enables the extension.

If the indicator doesn't appear after install:
- **X11** — press `Alt+F2`, type `r`, press Enter (restarts GNOME Shell in-place)
- **Wayland** — log out and log back in

## Manual install

```bash
UUID="codelight@sensnology.se"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

mkdir -p "$DEST/schemas"
cp metadata.json extension.js prefs.js "$DEST/"
cp schemas/org.gnome.shell.extensions.codelight.gschema.xml "$DEST/schemas/"
glib-compile-schemas "$DEST/schemas/"
gnome-extensions enable "$UUID"
```

## Configuration

The defaults (localhost:8765, no secret) work out of the box when the daemon runs
on the same machine. To change settings, click the indicator → **Settings…**, or:

```bash
gnome-extensions prefs codelight@sensnology.se
```

| Setting | Default | Description |
|---------|---------|-------------|
| Host | `localhost` | Hostname or IP of the machine running `codelight.py` |
| Port | `8765` | WebSocket port |
| Secret | *(empty)* | Must match `--secret` passed to `codelight.py` |

## Start the daemon

```bash
# Same machine — no configuration needed:
python3 companion/codelight.py --name my-laptop

# Remote machine — set a secret and open the firewall on port 8765:
python3 companion/codelight.py --name my-laptop --secret mypassword
```

See [companion/README.md](../companion/README.md) for running as a systemd service.

## Reload after changes

```bash
bash gnome-extension/reload.sh
```

## Uninstall

```bash
gnome-extensions disable codelight@sensnology.se
rm -rf "$HOME/.local/share/gnome-shell/extensions/codelight@sensnology.se"
```
