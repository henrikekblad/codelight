import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DBUS_NAME  = 'se.sensnology.codelight';
const DBUS_PATH  = '/se/sensnology/codelight';
const DBUS_IFACE = 'se.sensnology.codelight';

const IFACE_XML = `<node>
  <interface name="se.sensnology.codelight">
    <signal name="StatusChanged">
      <arg type="s" name="status_json"/>
    </signal>
    <method name="GetStatus">
      <arg direction="out" type="s"/>
    </method>
  </interface>
</node>`;

// Colors matching Android widget / ESP8266 screen
const C = {
    working:  [1.000, 0.549, 0.000],   // #FF8C00
    waiting:  [1.000, 0.133, 0.000],   // #FF2200
    inactive: [0.000, 0.784, 0.000],   // #00C800
    offline:  [0.333, 0.333, 0.333],   // #555555
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
        this._indicator = new PanelMenu.Button(0.0, 'Codelight', false);

        // ── Panel button ────────────────────────────────────────────────────
        const panelBox = new St.BoxLayout({ style_class: 'panel-status-menu-box' });
        this._panelIcon = new St.Icon({
            gicon: Gio.icon_new_for_string(this.path + '/icons/claude-symbolic.svg'),
            icon_size: 16,
            style_class: 'system-status-icon',
        });
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
        if (this._signalId !== null && this._proxy !== null) {
            this._proxy.disconnectSignal(this._signalId);
            this._signalId = null;
        }
        this._proxy = null;
        this._setOffline();
    }

    _handleMessage(data) {
        const status   = data?.status ?? 'offline';
        const color    = C[status] ?? C.offline;
        const hex      = toHex(color);
        const sessions = data?.sessions ?? 0;
        const label    = status.toUpperCase();

        this._panelIcon.set_style(`color: ${hex};`);

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
        this._panelIcon.set_style(`color: ${hex};`);
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
        if (this._signalId !== null && this._proxy !== null) {
            this._proxy.disconnectSignal(this._signalId);
            this._signalId = null;
        }
        this._proxy = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
