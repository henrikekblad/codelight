// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// Usage meter row: label, %, reset, and a codelight green-to-red bar.

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3

ColumnLayout {
    id: bar

    property string label: ""
    property real pct: 0.0
    property string reset: ""
    // Resolved palette mode from plasmoidItem.effectiveColorMode.
    property string colorMode: "dark"
    // Chrome colors from plasmoidItem's palette.
    property color textColor: "#eff0ec"
    property color disabledTextColor: "#8a8a8a"

    spacing: Kirigami.Units.smallSpacing
    Layout.fillWidth: true

    // The daemon passes agent-reported percentages straight through without
    // clamping, so every consumer of `pct` clamps for itself.
    readonly property real clampedPct: Math.max(0, Math.min(1, bar.pct))

    // codelight gradient (green->yellow->orange->red) per pct, per palette.
    function usageColor(p) {
        const mode = bar.colorMode === "light" || bar.colorMode === "highcontrast" ? bar.colorMode : "dark";
        const stops = mode === "light" ? [[0.0, 0.0, 0.55, 0.0], [0.5, 0.72, 0.6, 0.0], [0.75, 0.9, 0.43, 0.0], [1.0, 0.78, 0.08, 0.0]] : mode === "highcontrast" ? [[0.0, 0.0, 0.69, 0.0], [0.5, 0.5, 0.5, 0.0], [0.75, 1.0, 0.44, 0.0], [1.0, 0.82, 0.0, 0.0]] : [[0.0, 0.0, 0.784, 0.0], [0.5, 1.0, 1.0, 0.0], [0.75, 1.0, 0.549, 0.0], [1.0, 1.0, 0.133, 0.0]];
        const x = Math.max(0, Math.min(1, p));
        for (let i = 0; i < 3; i++) {
            if (x <= stops[i + 1][0]) {
                const a = stops[i];
                const b = stops[i + 1];
                const t = (x - a[0]) / (b[0] - a[0]);
                return Qt.rgba(a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]), a[3] + t * (b[3] - a[3]), 1);
            }
        }
        return Qt.rgba(stops[3][1], stops[3][2], stops[3][3], 1);
    }

    RowLayout {
        Layout.fillWidth: true

        PlasmaComponents3.Label {
            text: bar.label
            color: bar.textColor
        }

        PlasmaComponents3.Label {
            text: Math.round(bar.clampedPct * 100) + "%"
            font.bold: true
            color: bar.textColor
        }

        Item {
            Layout.fillWidth: true
        }

        PlasmaComponents3.Label {
            text: bar.reset ? "↻ " + bar.reset : ""
            color: bar.disabledTextColor
        }
    }

    // Custom bar so the fill can carry the gradient; track uses translucent text color.
    Item {
        Layout.fillWidth: true
        Layout.preferredHeight: Kirigami.Units.gridUnit * 0.5

        Rectangle {
            id: track
            anchors.fill: parent
            radius: height / 2
            color: Qt.rgba(bar.textColor.r, bar.textColor.g, bar.textColor.b, 0.15)
            border.width: 1
            border.color: Qt.rgba(bar.textColor.r, bar.textColor.g, bar.textColor.b, 0.25)

            Rectangle {
                id: fill
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                radius: parent.radius
                width: bar.clampedPct * parent.width
                color: bar.usageColor(bar.clampedPct)
            }
        }
    }
}
