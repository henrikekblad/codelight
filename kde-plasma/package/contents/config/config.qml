// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// The applet's config page model (the package's "configmodel" file). Plasma's
// ConfigView loads this to discover the applet's own config pages; without it
// only the shell's standard pages (Shortcuts, About) appear. Each
// ConfigCategory's `source` is relative to contents/ui/, so "config/…"
// resolves to contents/ui/config/… .

import QtQuick

import org.kde.plasma.configuration

ConfigModel {
    ConfigCategory {
        name: "General"
        icon: "configure"
        source: "config/ConfigGeneral.qml"
    }
}
