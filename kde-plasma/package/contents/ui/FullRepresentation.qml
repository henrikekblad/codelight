// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// Popup and desktop view: one block per seen agent (logo, status, usage bars,
// session count on the active row); offline placeholder otherwise.
//
// Uses a Flickable + ColumnLayout (ListView delegate layouts collapse heights).

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasmoid

PlasmaExtras.Representation {
    id: full

    // The PlasmoidItem root holds the model (var: custom props not in qmltypes).
    required property var plasmoidItem

    collapseMarginsHint: true

    Layout.preferredWidth: Kirigami.Units.gridUnit * 24
    Layout.preferredHeight: Kirigami.Units.gridUnit * 22
    // The shell reads these off the full representation to bound the popup and
    // the desktop widget; without them both default to 0 and it can be dragged
    // down to nothing.
    Layout.minimumWidth: Kirigami.Units.gridUnit * 14
    Layout.minimumHeight: Kirigami.Units.gridUnit * 8
    Layout.fillWidth: true
    Layout.fillHeight: true

    readonly property var status: full.plasmoidItem.status
    readonly property int sessions: full.status && typeof full.status.sessions === "number" ? full.status.sessions : 0
    readonly property string sessionsLabel: full.sessions === 1 ? "1 session" : full.sessions + " sessions"
    readonly property real hMargin: Kirigami.Units.gridUnit
    // `Plasmoid.action()` isn't in the qmltypes; hold Plasmoid in a var.
    readonly property var plasmoidRef: Plasmoid
    // False once the user unticks "Show background" on a desktop widget
    // (NoBackground is 0, so this tests for the drawing bits rather than masking).
    readonly property bool containmentDrawsBackground: (Plasmoid.effectiveBackgroundHints & (PlasmaCore.Types.StandardBackground | PlasmaCore.Types.TranslucentBackground)) !== 0
    // System tray draws its own heading + configure button; hide ours then.
    readonly property bool containmentHasHeading: (Plasmoid.containmentDisplayHints & PlasmaCore.Types.ContainmentDrawsPlasmoidHeading) !== 0
    readonly property bool showConfigureButton: !full.containmentHasHeading

    function agentStatusForId(agentId) {
        const pas = full.status ? full.status.per_agent_status : null;
        return pas && pas[agentId] ? full.plasmoidItem.normalizeStatus(pas[agentId]) : "";
    }

    // Only paint a background for an explicit palette override. On "System
    // Default" the Plasma dialog already draws (and blurs) its own background;
    // covering it with an opaque rectangle fights Breeze. And if the user has
    // switched the widget's background off entirely, honor that in every mode
    // — otherwise our rectangle would make the toggle look broken.
    Rectangle {
        anchors.fill: parent
        z: -1
        visible: !full.plasmoidItem.useSystemPalette && full.containmentDrawsBackground
        color: full.plasmoidItem.paletteBackground
    }

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: flick.width
        contentHeight: contentCol.implicitHeight
        boundsBehavior: Flickable.StopAtBounds

        PlasmaComponents3.ScrollBar.vertical: PlasmaComponents3.ScrollBar {
            active: true
        }

        ColumnLayout {
            id: contentCol
            width: flick.width
            spacing: Kirigami.Units.largeSpacing

            // ── Offline placeholder ──────────────────────────────────────
            PlasmaExtras.PlaceholderMessage {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.gridUnit * 4
                Layout.leftMargin: full.hMargin
                Layout.rightMargin: full.hMargin
                visible: !full.plasmoidItem.online
                iconName: "network-disconnect"
                text: "codelight daemon offline"
                explanation: "Start the companion daemon with the dbus-fast Python package installed"
            }

            // Top inset so the first agent block clears the plasmoid header.
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: full.plasmoidItem.online ? full.hMargin : 0
            }

            // ── One block per reported agent ─────────────────────────────
            Repeater {
                model: full.plasmoidItem.agentOrder
                delegate: ColumnLayout {
                    id: agentBlock
                    required property string modelData
                    readonly property string agentId: agentBlock.modelData

                    // Per-agent status ("" for unseen agents); icon tints with it.
                    readonly property string perStatus: full.agentStatusForId(agentBlock.agentId)
                    readonly property string iconStatus: agentBlock.perStatus || "offline"
                    readonly property string statusText: agentBlock.perStatus ? agentBlock.perStatus.toUpperCase() : ""
                    readonly property bool isActive: agentBlock.agentId === full.plasmoidItem.activeAgentId

                    readonly property var usage: {
                        const pau = full.status ? full.status.per_agent_usage : null;
                        return pau && pau[agentBlock.agentId] ? pau[agentBlock.agentId] : null;
                    }
                    // The key is omitted (not []) when an agent has no window.
                    // Capped at two, matching the GNOME client.
                    readonly property var limits: agentBlock.usage && Array.isArray(agentBlock.usage.limits) ? agentBlock.usage.limits.slice(0, 2) : []

                    Layout.fillWidth: true
                    Layout.leftMargin: full.hMargin
                    Layout.rightMargin: full.hMargin
                    spacing: Kirigami.Units.smallSpacing
                    visible: full.plasmoidItem.online

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        // Agent logo tinted with that agent's per-agent
                        // status (or grey when the agent isn't seen).
                        Image {
                            source: {
                                // Explicit dep so the icon re-tints on color-mode change.
                                full.plasmoidItem.effectiveColorMode;
                                return full.plasmoidItem.logoSource(agentBlock.agentId, agentBlock.iconStatus);
                            }
                            sourceSize.width: 128
                            sourceSize.height: 128
                            fillMode: Image.PreserveAspectFit
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 1.2
                            Layout.preferredHeight: Kirigami.Units.gridUnit * 1.2
                            Layout.alignment: Qt.AlignVCenter
                        }

                        PlasmaComponents3.Label {
                            text: full.plasmoidItem.agentDisplay(agentBlock.agentId)
                            font.bold: true
                            color: full.plasmoidItem.paletteText
                            Layout.fillWidth: true
                        }

                        // Total active session count (active agent only).
                        PlasmaComponents3.Label {
                            text: full.sessionsLabel
                            color: full.plasmoidItem.paletteDisabled
                            visible: agentBlock.isActive
                        }

                        // Wider gap before the status (active only).
                        Item {
                            Layout.preferredWidth: agentBlock.isActive ? Kirigami.Units.largeSpacing : 0
                            visible: agentBlock.isActive
                        }

                        // Fixed-width, right-aligned so the session count never shifts.
                        PlasmaComponents3.Label {
                            text: agentBlock.statusText
                            color: {
                                full.plasmoidItem.effectiveColorMode;
                                return agentBlock.perStatus ? full.plasmoidItem.statusColor(agentBlock.perStatus) : full.plasmoidItem.paletteDisabled;
                            }
                            horizontalAlignment: Text.AlignRight
                            Layout.preferredWidth: statusRuler.implicitWidth
                        }
                    }

                    // Hidden ruler: keeps the status label width stable.
                    PlasmaComponents3.Label {
                        id: statusRuler
                        visible: false
                        text: "WORKING"
                    }

                    Repeater {
                        model: agentBlock.limits
                        delegate: UsageBar {
                            required property var modelData
                            Layout.fillWidth: true
                            colorMode: full.plasmoidItem.effectiveColorMode
                            textColor: full.plasmoidItem.paletteText
                            disabledTextColor: full.plasmoidItem.paletteDisabled
                            label: modelData && modelData.label ? String(modelData.label) : ""
                            pct: modelData && typeof modelData.pct === "number" ? modelData.pct : 0.0
                            reset: modelData && modelData.reset ? String(modelData.reset) : ""
                        }
                    }
                }
            }

            // Bottom inset so the last block clears the configure button.
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: (full.plasmoidItem.online && full.showConfigureButton) ? Kirigami.Units.gridUnit * 2 : 0
            }
        }
    }

    // Floating configure button (bottom-right).
    PlasmaComponents3.ToolButton {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Kirigami.Units.smallSpacing
        z: 1
        visible: full.showConfigureButton
        icon.name: "configure"
        Accessible.name: "Configure codelight"

        onClicked: {
            // "configure" is an internal action; use internalAction.
            const p = full.plasmoidRef;
            const a = (p.internalAction ? p.internalAction("configure") : null) || (p.action ? p.action("configure") : null);
            if (a) {
                a.trigger();
            }
        }
    }
}
