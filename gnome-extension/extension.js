import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DBUS_NAME  = 'se.sensnology.codelight';
const DBUS_PATH  = '/se/sensnology/codelight';
const DBUS_IFACE = 'se.sensnology.codelight';

const IFACE_XML = `<node>
  <interface name="se.sensnology.codelight">
    <signal name="StatusChanged">
      <arg type="s" name="status_json"/>
    </signal>
    <signal name="PermissionRequest">
      <arg type="s" name="request_json"/>
    </signal>
    <signal name="PermissionResolved">
      <arg type="s" name="resolved_json"/>
    </signal>
    <method name="GetStatus">
      <arg direction="out" type="s"/>
    </method>
    <method name="RespondPermission">
      <arg direction="in" type="s" name="request_id"/>
      <arg direction="in" type="s" name="decision"/>
      <arg direction="out" type="b"/>
    </method>
  </interface>
</node>`;

// Colors matching Android widget / ESP8266 screen
const C = {
    working:  [1.000, 0.549, 0.000],   // #FF8C00
    waiting:  [1.000, 0.133, 0.000],   // #FF2200
    idle:     [0.000, 0.784, 0.000],   // #00C800
    offline:  [0.533, 0.533, 0.533],   // #888888
    barBg:    [0.267, 0.267, 0.267],   // #444444
};

// Green→Yellow→Orange→Red gradient matching firmware usageColor()
// stops: #00C800 @0%, #FFFF00 @50%, #FF8C00 @75%, #FF2200 @100%
function usageColor(pct) {
    const stops = [
        [0.000, 0.784, 0.000],
        [1.000, 1.000, 0.000],
        [1.000, 0.549, 0.000],
        [1.000, 0.133, 0.000],
    ];
    const edges = [0.0, 0.5, 0.75, 1.0];
    const p = Math.max(0, Math.min(1, pct));
    for (let i = 0; i < 3; i++) {
        if (p <= edges[i + 1]) {
            const t = (p - edges[i]) / (edges[i + 1] - edges[i]);
            return stops[i].map((c, j) => c + t * (stops[i + 1][j] - c));
        }
    }
    return stops[3];
}

function toHex([r, g, b]) {
    return '#' + [r, g, b].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
}

// Draw a full-width rounded progress bar via Cairo
function drawBar(area, pct, fill, bg) {
    const cr = area.get_context();
    const [w, h] = area.get_surface_size();
    const rad = h / 2;

    function rrect(x, bw) {
        if (bw <= 0) return;
        const r = Math.min(rad, bw / 2);
        cr.newPath();
        cr.arc(x + r,      rad, r, Math.PI,      Math.PI * 1.5);
        cr.arc(x + bw - r, rad, r, Math.PI * 1.5, 0);
        cr.arc(x + bw - r, rad, r, 0,             Math.PI * 0.5);
        cr.arc(x + r,      rad, r, Math.PI * 0.5, Math.PI);
        cr.closePath();
    }

    cr.setSourceRGB(...bg);
    rrect(0, w);
    cr.fill();

    const fw = Math.max(0, Math.min(pct, 1) * w);
    if (fw > 1) {
        cr.setSourceRGB(...fill);
        rrect(0, fw);
        cr.fill();
    }

    cr.$dispose();
}

function makeMeterItem(label) {
    const item = new PopupMenu.PopupBaseMenuItem({ reactive: false });

    const col = new St.BoxLayout({ vertical: true, x_expand: true });

    const row = new St.BoxLayout({ x_expand: true });
    const lbl = new St.Label({
        text: label + ' — ',
        style: 'color: #eeeeee; font-size: 11px; width: 58px;',
        y_align: Clutter.ActorAlign.CENTER,
    });
    const pct = new St.Label({
        text: '0%',
        style: 'color: #eeeeee; font-size: 11px; font-weight: bold;',
        y_align: Clutter.ActorAlign.CENTER,
    });
    const rst = new St.Label({
        text: '↻ --',
        style: 'color: #888888; font-size: 10px;',
        x_expand: true,
        x_align: Clutter.ActorAlign.END,
        y_align: Clutter.ActorAlign.CENTER,
    });
    row.add_child(lbl);
    row.add_child(pct);
    row.add_child(rst);

    const bar = new St.DrawingArea({ height: 7, x_expand: true, style: 'margin-top: 5px;' });
    bar._pct = 0;
    bar.connect('repaint', a => drawBar(a, a._pct, usageColor(a._pct), C.barBg));

    col.add_child(row);
    col.add_child(bar);
    item.add_child(col);

    item._pctLabel = pct;
    item._rstLabel = rst;
    item._bar      = bar;
    return item;
}

export default class CodelightExtension extends Extension {
    enable() {
        this._proxy    = null;
        this._signalId = null;
        this._watchId  = null;
        this._settings = this.getSettings();
        this._permSignalIds = [];
        this._permNotifs    = new Map();   // request id → MessageTray.Notification
        this._notifSource   = null;
        this._indicator = new PanelMenu.Button(0.0, 'Codelight', false);

        // ── Panel button ────────────────────────────────────────────────────
        const panelBox = new St.BoxLayout({ style_class: 'panel-status-menu-box' });
        this._panelIcon = new St.Icon({ icon_size: 16, style_class: 'system-status-icon' });
        panelBox.add_child(this._panelIcon);
        this._indicator.add_child(panelBox);

        // ── Popup ────────────────────────────────────────────────────────────

        // Status header row
        const hdrItem = new PopupMenu.PopupBaseMenuItem({ reactive: false });
        const hdrBox  = new St.BoxLayout({ x_expand: true, style: 'spacing: 8px;' });

        this._hdrDot    = new St.Label({ style: 'font-size: 20px;', y_align: Clutter.ActorAlign.CENTER });
        this._hdrStatus = new St.Label({
            style: 'color: #eeeeee; font-size: 14px; font-weight: bold;',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._hdrSessions = new St.Label({
            style: 'color: #888888; font-size: 11px;',
            x_expand: true,
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
        });
        hdrBox.add_child(this._hdrDot);
        hdrBox.add_child(this._hdrStatus);
        hdrBox.add_child(this._hdrSessions);
        hdrItem.add_child(hdrBox);
        this._indicator.menu.addMenuItem(hdrItem);

        // Meter rows
        this._weeklyItem  = makeMeterItem('Weekly');
        this._sessionItem = makeMeterItem('Session');
        this._indicator.menu.addMenuItem(this._weeklyItem);
        this._indicator.menu.addMenuItem(this._sessionItem);

        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this._setOffline();

        this._watchId = Gio.bus_watch_name(
            Gio.BusType.SESSION,
            DBUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            () => this._onDaemonAppeared(),
            () => this._onDaemonVanished()
        );
    }

    _onDaemonAppeared() {
        try {
            const nodeInfo = Gio.DBusNodeInfo.new_for_xml(IFACE_XML);
            this._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                nodeInfo.interfaces[0],
                DBUS_NAME, DBUS_PATH, DBUS_IFACE,
                null
            );
            this._signalId = this._proxy.connectSignal('StatusChanged', (_proxy, _sender, [json]) => {
                try {
                    const data = JSON.parse(json);
                    if (data?.type === 'config') return;
                    this._handleMessage(data);
                } catch (_) {}
            });
            this._permSignalIds.push(this._proxy.connectSignal('PermissionRequest',
                (_proxy, _sender, [json]) => this._onPermissionRequest(json)));
            this._permSignalIds.push(this._proxy.connectSignal('PermissionResolved',
                (_proxy, _sender, [json]) => this._onPermissionResolved(json)));
            // Fetch current state immediately so the panel isn't blank on connect
            try {
                const result = this._proxy.call_sync('GetStatus', null, Gio.DBusCallFlags.NONE, -1, null);
                const [json] = result.deepUnpack();
                const data = JSON.parse(json);
                if (data?.type !== 'config') this._handleMessage(data);
            } catch (_) {}
        } catch (e) {
            logError(e, 'codelight D-Bus connect failed');
        }
    }

    _onDaemonVanished() {
        this._disconnectProxy();
        this._destroyAllPermNotifs();
        this._setOffline();
    }

    _disconnectProxy() {
        if (this._proxy !== null) {
            if (this._signalId !== null)
                this._proxy.disconnectSignal(this._signalId);
            for (const id of this._permSignalIds)
                this._proxy.disconnectSignal(id);
        }
        this._signalId = null;
        this._permSignalIds = [];
        this._proxy = null;
    }

    // ── Permission approval ──────────────────────────────────────────────────

    _getNotifSource() {
        if (this._notifSource) return this._notifSource;
        let source;
        try {
            source = new MessageTray.Source({
                title: 'Claude Code',
                iconName: 'dialog-question-symbolic',
            });
        } catch (_) {
            // GNOME 45 positional constructor
            source = new MessageTray.Source('Claude Code', 'dialog-question-symbolic');
        }
        source.connect('destroy', () => { this._notifSource = null; });
        Main.messageTray.add(source);
        this._notifSource = source;
        return source;
    }

    _onPermissionRequest(json) {
        let req;
        try { req = JSON.parse(json); } catch (_) { return; }
        if (!req?.id || this._permNotifs.has(req.id)) return;
        if (!this._settings.get_boolean('permission-prompts')) return;

        const source = this._getNotifSource();
        const body   = req.summary || req.tool_name || 'tool use';
        let n;
        try {
            n = new MessageTray.Notification({
                source,
                title: 'Claude Code asks',
                body,
                urgency: MessageTray.Urgency.CRITICAL,   // stays until answered
            });
        } catch (_) {
            // GNOME 45 positional constructor
            n = new MessageTray.Notification(source, 'Claude Code asks', body);
            n.setUrgency?.(MessageTray.Urgency.CRITICAL);
        }
        n.addAction('Allow', () => this._respondPermission(req.id, 'allow'));
        n.addAction('Deny',  () => this._respondPermission(req.id, 'deny'));
        n.connect('destroy', () => this._permNotifs.delete(req.id));
        this._permNotifs.set(req.id, n);
        if (source.addNotification)
            source.addNotification(n);
        else
            source.showNotification(n);   // GNOME 45
    }

    _respondPermission(id, decision) {
        try {
            this._proxy?.call_sync('RespondPermission',
                new GLib.Variant('(ss)', [id, decision]),
                Gio.DBusCallFlags.NONE, -1, null);
        } catch (e) {
            logError(e, 'codelight RespondPermission failed');
        }
        this._destroyPermNotif(id);
    }

    _onPermissionResolved(json) {
        try {
            this._destroyPermNotif(JSON.parse(json)?.id);
        } catch (_) {}
    }

    _destroyPermNotif(id) {
        const n = this._permNotifs.get(id);
        if (!n) return;
        this._permNotifs.delete(id);
        n.destroy();
    }

    _destroyAllPermNotifs() {
        for (const id of [...this._permNotifs.keys()])
            this._destroyPermNotif(id);
    }

    _handleMessage(data) {
        let status = data?.status ?? 'offline';
        if (status === 'inactive') status = 'idle';   // companions < 1.0.9
        const color    = C[status] ?? C.offline;
        const hex      = toHex(color);
        const sessions = data?.sessions ?? 0;
        const label    = status.toUpperCase();

        // statuses without an icon of their own fall back to the offline icon
        this._panelIcon.gicon = Gio.icon_new_for_string(
            `${this.path}/icons/claude-${C[status] ? status : 'offline'}.svg`);

        this._hdrDot.set_style(`color: ${hex};`);
        this._hdrDot.set_text('●');
        this._hdrStatus.set_text(label);
        this._hdrSessions.set_text(
            sessions === 1 ? '1 session' : `${sessions} sessions`
        );

        this._setMeter(this._weeklyItem,  data?.weekly_pct,  data?.weekly_reset);
        this._setMeter(this._sessionItem, data?.session_pct, data?.session_reset);
    }

    _setMeter(item, pct, reset) {
        item._pctLabel.set_text(`${Math.round((pct ?? 0) * 100)}%`);
        item._rstLabel.set_text(`↻ ${reset || '--'}`);
        item._bar._pct = pct ?? 0;
        item._bar.queue_repaint();
    }

    _setOffline() {
        const hex = toHex(C.offline);
        this._panelIcon.gicon = Gio.icon_new_for_string(`${this.path}/icons/claude-offline.svg`);
        this._hdrDot.set_style(`color: ${hex};`);
        this._hdrDot.set_text('●');
        this._hdrStatus.set_text('OFFLINE');
        this._hdrSessions.set_text('daemon offline');
        this._setMeter(this._weeklyItem,  null, null);
        this._setMeter(this._sessionItem, null, null);
    }

    disable() {
        if (this._watchId !== null) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = null;
        }
        this._disconnectProxy();
        this._destroyAllPermNotifs();
        this._notifSource?.destroy();
        this._notifSource = null;
        this._settings = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
