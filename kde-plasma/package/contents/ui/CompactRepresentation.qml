// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// Panel/tray icon: the active agent's tinted logo; click toggles the popup.

pragma ComponentBehavior: Bound

import QtQuick

Item {
    id: compact

    // The PlasmoidItem (root of main.qml) holds the model and helpers.
    required property var plasmoidItem

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        onClicked: compact.plasmoidItem.expanded = !compact.plasmoidItem.expanded
    }

    Image {
        anchors.centerIn: parent
        width: Math.min(parent.width, parent.height)
        height: width
        // High-res raster cache so the SVG stays crisp at any panel size / DPI.
        sourceSize.width: 128
        sourceSize.height: 128
        fillMode: Image.PreserveAspectFit
        source: compact.plasmoidItem.logoSource(compact.plasmoidItem.activeAgentId, compact.plasmoidItem.activeStatus)
    }

    Accessible.name: compact.plasmoidItem.agentDisplay(compact.plasmoidItem.activeAgentId) + " " + compact.plasmoidItem.activeStatus
    Accessible.description: "codelight coding-agent status"
    Accessible.role: Accessible.Button
    Accessible.onPressAction: compact.plasmoidItem.expanded = !compact.plasmoidItem.expanded
}
