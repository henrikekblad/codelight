#!/usr/bin/env bash
# Run from the gnome-extension/ directory.
set -euo pipefail

UUID="codelight@sensnology.se"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

echo "Installing codelight GNOME extension to $DEST"

mkdir -p "$DEST/schemas" "$DEST/icons"
cp metadata.json extension.js prefs.js "$DEST/"
cp icons/*.svg "$DEST/icons/"
cp schemas/org.gnome.shell.extensions.codelight.gschema.xml "$DEST/schemas/"
glib-compile-schemas "$DEST/schemas/"

if gnome-extensions enable "$UUID" 2>/dev/null; then
    echo "Extension enabled."
else
    echo "Could not auto-enable. Enable it manually:"
    echo "  gnome-extensions enable $UUID"
    echo "  — or use the GNOME Extensions app"
fi

echo ""
echo "If the indicator doesn't appear in the top bar:"
echo "  X11    → Alt+F2, type 'r', Enter  (restarts GNOME Shell in-place)"
echo "  Wayland → log out and log back in"
