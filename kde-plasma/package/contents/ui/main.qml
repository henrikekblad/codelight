// SPDX-FileCopyrightText: 2026 Henrik Ekblad
// SPDX-FileCopyrightText: 2026 John Venice <john@johnvenice.dev>
// SPDX-License-Identifier: MIT
//
// codelight Plasma applet: owns the daemon model and D-Bus lifecycle.

pragma ComponentBehavior: Bound

import QtQuick

import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasmoid
import org.kde.plasma.workspace.dbus as DBus

PlasmoidItem {
    id: root

    readonly property string dbusService: "se.sensnology.codelight"
    readonly property string dbusPath: "/se/sensnology/codelight"
    readonly property string dbusIface: "se.sensnology.codelight"

    // ── Model ────────────────────────────────────────────────────────────
    // Status/agents are retained on disappearance so labels stay stable offline.
    property bool online: false
    property var status: ({})
    property var agents: ({})
    property string defaultAgentId: ""

    // Bumped on every appear/vanish. Async replies carry the generation they
    // were issued under and are dropped if the daemon has since bounced —
    // otherwise a late reply resurrects a connection that no longer exists.
    property int connectionGeneration: 0
    // Separate from `online`: `online` is connection state, this is the
    // one-shot branding fetch guard. Conflating them meant a late status reply
    // could set `online` first and make the next appearance skip GetConfig.
    property bool configFetched: false

    // Fallback logo; uses currentColor so it tints with the status color.
    readonly property string genericLogoSvg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">' + '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="2"/>' + '</svg>'

    readonly property string activeAgentId: {
        const s = root.status;
        if (s && s.agent_id)
            return String(s.agent_id);
        if (s && s.last_active_agent)
            return String(s.last_active_agent);
        return root.defaultAgentId;
    }

    // Older daemons emit "inactive" where current ones emit "idle"; the GNOME
    // client normalizes the same way (extension.js `normalizedStatus`).
    function normalizeStatus(s) {
        const v = String(s || "").toLowerCase();
        return v === "inactive" ? "idle" : v;
    }

    readonly property string activeStatus: root.online ? (root.status && root.status.status ? root.normalizeStatus(root.status.status) : "idle") : "offline"

    // Seen agents only (usage first, then status-only); default first, then alpha.
    readonly property var agentOrder: {
        const pas = root.status ? root.status.per_agent_status : null;
        const pau = root.status ? root.status.per_agent_usage : null;
        const usageIds = pau ? new Set(Object.keys(pau)) : new Set();
        const statusIds = pas ? new Set(Object.keys(pas)) : new Set();

        const tier1 = []; // has usage
        const tier2 = []; // seen, no usage
        for (const id of usageIds)
            tier1.push(id);
        for (const id of statusIds)
            if (!usageIds.has(id))
                tier2.push(id);

        const alpha = (a, b) => a < b ? -1 : (a > b ? 1 : 0);
        const sortTier = tier => {
            const d = root.defaultAgentId;
            tier.sort(alpha);
            if (d && tier.includes(d)) {
                const i = tier.indexOf(d);
                if (i > 0) {
                    tier.splice(i, 1);
                    tier.unshift(d);
                }
            }
            return tier;
        };
        return sortTier(tier1).concat(sortTier(tier2));
    }

    // ── Plasmoid presentation ───────────────────────────────────────────
    // `Plasmoid.icon` is only the fallback; the compact rep draws the logo.
    Plasmoid.icon: "codelight"
    Plasmoid.title: "codelight"
    // Lets the user toggle the widget's background off from its own config
    // (Plasma adds the checkbox itself — no setting of ours). Same one-liner
    // org.kde.plasma.systemmonitor uses. Desktop placement only; a panel popup
    // always gets the shell's dialog background.
    Plasmoid.backgroundHints: PlasmaCore.Types.DefaultBackground | PlasmaCore.Types.ConfigurableBackground

    // Live tray/panel tooltip: agent + status, sessions, or the offline hint.
    readonly property int sessionsCount: root.status && typeof root.status.sessions === "number" ? root.status.sessions : 0
    toolTipMainText: root.online ? root.agentDisplay(root.activeAgentId) + " " + root.activeStatus.toUpperCase() : "codelight offline"
    toolTipSubText: root.online ? (root.sessionsCount === 1 ? "1 session" : root.sessionsCount + " sessions") : "Start the companion daemon"

    // Palette override: "system" | "light" | "dark" | "highcontrast".
    readonly property string themeMode: String(Plasmoid.configuration.theme || "system")
    Plasmoid.status: root.online ? PlasmaCore.Types.ActiveStatus : (Plasmoid.configuration.alwaysVisible ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus)

    compactRepresentation: CompactRepresentation {
        plasmoidItem: root
    }
    fullRepresentation: FullRepresentation {
        plasmoidItem: root
    }

    // ── D-Bus lifecycle ─────────────────────────────────────────────────
    DBus.DBusServiceWatcher {
        id: daemonWatcher
        busType: DBus.BusType.Session
        watchedService: root.dbusService
    }

    Connections {
        target: daemonWatcher
        function onRegisteredChanged() {
            if (daemonWatcher.registered) {
                root.onDaemonAppeared();
            } else {
                root.onDaemonVanished();
            }
        }
    }

    // If the daemon is already present, `registered` may init true without a signal.
    Component.onCompleted: {
        if (daemonWatcher.registered) {
            root.onDaemonAppeared();
        }
    }

    // Live `StatusChanged` push, when the shell is new enough to provide it.
    // `DBus.SignalWatcher` is Plasma 6.4+; the module around it is 6.2+. It
    // lives in its own file so a missing type is a Loader error rather than a
    // compile error that would take the whole applet down — see StatusWatcher.qml.
    property bool signalWatcherAvailable: true

    Loader {
        id: watcherLoader
        // Deliberately NOT gated on signalWatcherAvailable: this handler writes
        // that flag, so binding `active` to it is a loop. An errored Loader is
        // inert, so leaving it active costs nothing.
        active: root.online
        source: "StatusWatcher.qml"

        onLoaded: {
            // Assigning all three completes the watcher and connects it.
            watcherLoader.item.service = root.dbusService;
            watcherLoader.item.path = root.dbusPath;
            watcherLoader.item.iface = root.dbusIface;
        }

        onStatusChanged: {
            if (watcherLoader.status === Loader.Error && root.signalWatcherAvailable) {
                root.signalWatcherAvailable = false;
                console.warn("[codelight] SignalWatcher unavailable (needs Plasma 6.4+); polling GetStatus instead");
            }
        }
    }

    Connections {
        target: watcherLoader.item
        ignoreUnknownSignals: true
        function onStatusReceived(json) {
            root.applyStatus(json, root.connectionGeneration);
        }
    }

    // Fallback for Plasma 6.2/6.3. The daemon recomputes status on a 2 s tick,
    // so polling at the same rate loses nothing but the push latency.
    Timer {
        interval: 2000
        repeat: true
        running: root.online && !root.signalWatcherAvailable
        onTriggered: root.fetchStatus()
    }

    // ── D-Bus calls ─────────────────────────────────────────────────────
    function onDaemonAppeared() {
        // onCompleted and the watcher can both fire; avoid a double fetch.
        if (root.configFetched) {
            return;
        }
        root.connectionGeneration += 1;
        root.online = true;
        root.configFetched = true;
        root.fetchConfig();
    }

    function onDaemonVanished() {
        root.connectionGeneration += 1;
        root.online = false;
        root.configFetched = false;
    }

    // Async replies are only valid while the daemon we called is still there.
    function replyIsCurrent(generation) {
        return generation === root.connectionGeneration && daemonWatcher.registered;
    }

    function fetchConfig() {
        const generation = root.connectionGeneration;
        DBus.SessionBus.asyncCall({
            "service": root.dbusService,
            "path": root.dbusPath,
            "iface": root.dbusIface,
            "member": "GetConfig",
            "arguments": ["kde"]
        }, result => root.applyConfig(result.value, generation), result => {
            console.warn("[codelight] GetConfig failed:", result && result.error ? result.error.message : String(result));
            // Still fetch status so the widget isn't blank if branding fails.
            if (root.replyIsCurrent(generation)) {
                root.fetchStatus();
            }
        });
    }

    function applyConfig(json, generation) {
        if (!root.replyIsCurrent(generation)) {
            return;
        }
        let parsed;
        try {
            // result.value is a QVariant-wrapped string; JSON.parse handles it.
            parsed = JSON.parse(json);
        } catch (e) {
            console.warn("[codelight] applyConfig parse fail:", e);
            root.fetchStatus();
            return;
        }
        if (!parsed || parsed.type !== "config") {
            root.fetchStatus();
            return;
        }
        if (parsed.agents && typeof parsed.agents === "object") {
            root.agents = parsed.agents;
        }
        // Set even when `agents` is absent, or agent ordering loses its anchor.
        root.defaultAgentId = String(parsed.default_agent_id || "");
        root.fetchStatus();
    }

    function fetchStatus() {
        const generation = root.connectionGeneration;
        DBus.SessionBus.asyncCall({
            "service": root.dbusService,
            "path": root.dbusPath,
            "iface": root.dbusIface,
            "member": "GetStatus",
            "arguments": []
        }, result => root.applyStatus(result.value, generation), result => console.warn("[codelight] GetStatus failed:", result && result.error ? result.error.message : String(result)));
    }

    function applyStatus(jsonOrObj, generation) {
        if (!root.replyIsCurrent(generation)) {
            return;
        }
        let parsed = jsonOrObj;
        // Parse method returns (QVariant-wrapped) and signal strings; skip if already parsed.
        if (!(parsed && typeof parsed === "object" && parsed.status !== undefined)) {
            try {
                parsed = JSON.parse(parsed);
            } catch (e) {
                console.warn("[codelight] applyStatus parse fail:", e);
                return;
            }
        }
        if (!parsed || parsed.type === "config") {
            return;
        }
        root.status = parsed;
    }

    // ── Rendering helpers (shared by both representations) ─────────────
    // True when the user hasn't forced a palette, i.e. we defer to Plasma.
    readonly property bool useSystemPalette: root.themeMode !== "light" && root.themeMode !== "dark" && root.themeMode !== "highcontrast"

    // Theme override (light/dark/highcontrast), else the KDE color scheme.
    // Drives the *branded* colors (status, usage gradient) in every mode.
    readonly property string effectiveColorMode: {
        if (!root.useSystemPalette)
            return root.themeMode;
        return Application.styleHints.colorScheme === Qt.Light ? "light" : "dark";
    }

    // Widget chrome. On "System Default" this is Plasma's own color scheme, so
    // custom and accessibility schemes are honored and the popup keeps its
    // translucent Breeze background; the fixed codelight palette is used only
    // when the user explicitly picks Light/Dark/High Contrast.
    Kirigami.Theme.colorSet: Kirigami.Theme.Window
    Kirigami.Theme.inherit: false

    readonly property color paletteBackground: root.useSystemPalette ? Kirigami.Theme.backgroundColor : root.effectiveColorMode === "light" ? "#eff0ec" : root.effectiveColorMode === "highcontrast" ? "#000000" : "#232629"
    readonly property color paletteText: root.useSystemPalette ? Kirigami.Theme.textColor : root.effectiveColorMode === "light" ? "#232629" : root.effectiveColorMode === "highcontrast" ? "#ffffff" : "#eff0ec"
    readonly property color paletteDisabled: root.useSystemPalette ? Kirigami.Theme.disabledTextColor : root.effectiveColorMode === "light" ? "#7a7a7a" : root.effectiveColorMode === "highcontrast" ? "#b0b0b0" : "#8a8a8a"

    function statusColor(s) {
        const m = root.effectiveColorMode;
        const dark = {
            "working": "#FF8C00",
            "waiting": "#FF2200",
            "idle": "#00C800",
            "offline": "#888888"
        };
        const light = {
            "working": "#E07000",
            "waiting": "#C40000",
            "idle": "#1E8E3E",
            "offline": "#8A8A8A"
        };
        const hc = {
            "working": "#FF8C00",
            "waiting": "#E00000",
            "idle": "#00A000",
            "offline": "#666666"
        };
        const map = m === "light" ? light : m === "highcontrast" ? hc : dark;
        return map[String(s)] || map["offline"];
    }

    function agentDisplay(agentId) {
        if (!agentId)
            return "codelight";
        const meta = root.agents ? root.agents[agentId] : null;
        if (meta && meta.display)
            return String(meta.display);
        const usage = root.status ? root.status.per_agent_usage : null;
        if (usage && usage[agentId] && usage[agentId].agent_display) {
            return String(usage[agentId].agent_display);
        }
        return agentId.charAt(0).toUpperCase() + agentId.slice(1);
    }

    // Tint the daemon SVG (currentColor) with the status color, as a data URL.
    function tintedSvg(agentId, statusStr) {
        const hex = root.statusColor(statusStr);
        let svg = "";
        const meta = root.agents ? root.agents[agentId] : null;
        if (meta && typeof meta.logo_svg === "string" && meta.logo_svg.indexOf("<svg") >= 0) {
            svg = meta.logo_svg;
        } else {
            svg = root.genericLogoSvg;
        }
        return svg.split("currentColor").join(hex);
    }

    function logoSource(agentId, statusStr) {
        return "data:image/svg+xml;utf8," + encodeURIComponent(root.tintedSvg(agentId, statusStr));
    }
}
