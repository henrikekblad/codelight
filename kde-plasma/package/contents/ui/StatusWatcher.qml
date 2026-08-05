// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// Live `StatusChanged` subscription, isolated in its own file on purpose.
//
// `DBus.SignalWatcher` only exists from Plasma 6.4; the surrounding
// `org.kde.plasma.workspace.dbus` module itself is there from 6.2. An unknown
// type is a *compile* error for the whole document, so keeping it here lets
// main.qml load it through a Loader and fall back to polling when the type is
// missing, instead of taking the entire applet down on Plasma 6.2/6.3.
//
// `service`/`path`/`iface` are assigned by the Loader's onLoaded; the watcher's
// setters (re)connect as soon as all three are set.

import QtQuick

import org.kde.plasma.workspace.dbus as DBus

DBus.SignalWatcher {
    id: watcher

    // Re-emitted to main.qml, which owns the model.
    signal statusReceived(string json)

    busType: DBus.BusType.Session

    // Handler name must be `dbus` + signal name (per Plasma's DBus module).
    function dbusStatusChanged(statusJson) {
        watcher.statusReceived(String(statusJson));
    }
}
