// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// Configuration page (discovered via contents/config/config.qml). Rooted in
// KCM.SimpleKCM (a Kirigami.ScrollablePage) so the config dialog gets a proper
// Page (no PageRow errors) and a scroll area.
//
// Values are exposed *only* as `cfg_<key>` properties. Plasma stages those and
// writes them to Plasmoid.configuration on Apply/OK, which is what makes
// Discard actually discard. Writing Plasmoid.configuration directly from the
// controls would give live preview at the cost of making Discard a no-op.
//
// Kirigami.FormLayout gives each control a real label association, so screen
// readers name them; a bare adjacent Label does not.

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.plasma.components as PlasmaComponents3

KCM.SimpleKCM {
    id: cfgGeneral

    title: "General"

    property string cfg_theme: "system"
    property alias cfg_alwaysVisible: alwaysVisibleBox.checked

    Kirigami.FormLayout {
        anchors.left: parent.left
        anchors.right: parent.right

        PlasmaComponents3.ComboBox {
            id: themeCombo

            Kirigami.FormData.label: "Theme:"

            textRole: "text"
            valueRole: "value"
            model: [
                {
                    "text": "System Default",
                    "value": "system"
                },
                {
                    "text": "Light",
                    "value": "light"
                },
                {
                    "text": "Dark",
                    "value": "dark"
                },
                {
                    "text": "High Contrast",
                    "value": "highcontrast"
                }
            ]
            onActivated: cfgGeneral.cfg_theme = themeCombo.currentValue
            Component.onCompleted: themeCombo.currentIndex = themeCombo.indexOfValue(cfgGeneral.cfg_theme)
        }

        PlasmaComponents3.CheckBox {
            id: alwaysVisibleBox

            Kirigami.FormData.label: "When the daemon is offline:"

            text: "Keep the icon visible"
        }

        PlasmaComponents3.Label {
            text: "Applies to the system tray, where the icon otherwise moves into the hidden-items popup until the daemon comes back."
            font: Kirigami.Theme.smallFont
            opacity: 0.7
            wrapMode: Text.WordWrap
            Layout.maximumWidth: Kirigami.Units.gridUnit * 20
        }
    }
}
