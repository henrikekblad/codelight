#!/usr/bin/env bash
# Install / upgrade the codelight Plasma applet for the current user.
# SPDX-License-Identifier: MIT
#
# Installs:
#   - the Plasma/Applet package (via kpackagetool6)
#   - a "codelight" theme icon into the user's hicolor icon theme, so the
#     widget has a proper icon for the window decoration / taskbar / Add
#     Widgets picker (a Plasma/Applet package can't ship a theme icon itself).
set -euo pipefail

cd "$(dirname "$0")"

PLASMOID_ID="se.sensnology.codelight"
ICON_SRC="icons/codelight.svg"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
ICON_DEST_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
ICON_DEST="$ICON_DEST_DIR/codelight.svg"

action="${1:-install}"

require_kpackagetool() {
  if ! command -v kpackagetool6 >/dev/null 2>&1; then
    echo "error: kpackagetool6 not found on \$PATH." >&2
    echo "       It ships with KF6's kpackage (Arch: kpackage, Debian/Ubuntu:" >&2
    echo "       libkf6package-bin, Fedora: kf6-kpackage). Install it and retry." >&2
    exit 1
  fi
}

is_installed() {
  kpackagetool6 --type Plasma/Applet --list 2>/dev/null | grep -qx "$PLASMOID_ID"
}

refresh_caches() {
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true
  fi
  if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
  fi
}

case "$action" in
  install|upgrade|"")
    require_kpackagetool
    echo "Installing codelight Plasma applet…"
    # Choose install vs upgrade by querying state rather than by failing into
    # it, so a real error (malformed package, permissions, missing dependency)
    # surfaces as itself instead of being retried as an upgrade.
    if is_installed; then
      kpackagetool6 --type Plasma/Applet --upgrade package
    else
      kpackagetool6 --type Plasma/Applet --install package
    fi

    # Install / refresh the theme icon.
    mkdir -p "$ICON_DEST_DIR"
    cp -f "$ICON_SRC" "$ICON_DEST"
    refresh_caches

    echo
    echo "Installed. Add it from Add Widgets, or run it in a window:"
    echo "  plasmawindowed $PLASMOID_ID"
    echo
    echo "plasmashell caches QML and the icon theme, so after installing or"
    echo "editing the applet, restart the shell to see the change:"
    echo "  systemctl --user restart plasma-plasmashell.service"
    echo
    echo "Reload after edits:  bash install.sh"
    echo "Remove:              bash install.sh remove"
    ;;
  remove|uninstall)
    require_kpackagetool
    echo "Removing codelight Plasma applet…"
    if is_installed; then
      # Any failure here is real; let it propagate rather than reporting success.
      kpackagetool6 --type Plasma/Applet --remove "$PLASMOID_ID"
    else
      echo "  (applet was not installed)"
    fi
    rm -f "$ICON_DEST"
    refresh_caches
    echo "Removed."
    ;;
  *)
    echo "Usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac
