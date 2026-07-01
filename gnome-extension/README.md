# codelight — GNOME Shell extension

A GNOME Shell extension that shows Claude Code status in the top bar.

<img src="../assets/gnome-extension.png" width="600" alt="codelight GNOME Shell extension">

The panel indicator shows **WORKING** (orange), **WAITING** (red), or **IDLE** (green).
Click it to see a popup with session and weekly token usage bars and the number of active
sessions. The extension connects to the companion daemon via **D-Bus** — no network socket
or configuration needed.

Requires **GNOME 45 or later** and the companion daemon from
[companion/README.md](../companion/README.md) running on the same machine.

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

## How it works

The extension watches for the D-Bus name `se.henrikekblad.codelight` on the session bus.
When `codelight.py` starts, the extension automatically connects, fetches the current
status, and subscribes to live `StatusChanged` signals. When the daemon stops, the
indicator shows **OFFLINE**.

No host, port, or secret settings are needed — the session bus is user-private and only
reachable by processes running as the same user.

## Start the daemon

```bash
python3 companion/codelight.py --name my-laptop
```

See [companion/README.md](../companion/README.md) for running as a systemd service.

## Reload after changes

```bash
bash gnome-extension/install.sh
```

## Uninstall

```bash
gnome-extensions disable codelight@sensnology.se
rm -rf "$HOME/.local/share/gnome-shell/extensions/codelight@sensnology.se"
```
