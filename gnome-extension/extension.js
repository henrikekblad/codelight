import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Soup from 'gi://Soup';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const RECONNECT_DELAY_S = 5;

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
        this._settings        = this.getSettings();
        this._ws              = null;
        this._authFailed      = false;
        this._reconnectTimer  = null;
        this._session         = new Soup.Session();
        this._indicator       = new PanelMenu.Button(0.0, 'Codelight', false);

        // ── Panel button ────────────────────────────────────────────────────
        const panelBox    = new St.BoxLayout({ style_class: 'panel-status-menu-box' });
        this._panelDot    = new St.Label({ y_expand: true, y_align: Clutter.ActorAlign.CENTER });
        this._panelStatus = new St.Label({ y_expand: true, y_align: Clutter.ActorAlign.CENTER });
        panelBox.add_child(this._panelDot);
        panelBox.add_child(this._panelStatus);
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

        // Settings link
        this._indicator.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const prefsItem = new PopupMenu.PopupMenuItem('Settings…');
        prefsItem.connect('activate', () => this.openPreferences());
        this._indicator.menu.addMenuItem(prefsItem);

        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this._settingsChangedId = this._settings.connect('changed', () => {
            this._authFailed = false;
            this._disconnect();
            this._scheduleReconnect(0);
        });

        this._setOffline();
        this._connect();
    }

    _connect() {
        if (this._authFailed)
            return;

        const host   = this._settings.get_string('host');
        const port   = this._settings.get_int('port');
        const secret = this._settings.get_string('secret');

        let message;
        try {
            message = Soup.Message.new('GET', `ws://${host}:${port}`);
        } catch (_) {
            this._scheduleReconnect();
            return;
        }

        this._session.websocket_connect_async(
            message, null, null, GLib.PRIORITY_DEFAULT, null,
            (session, result) => {
                if (!this._session) return;
                let ws;
                try {
                    ws = session.websocket_connect_finish(result);
                } catch (_) {
                    this._scheduleReconnect();
                    return;
                }

                this._ws = ws;

                if (secret)
                    ws.send_text(JSON.stringify({auth: secret}));

                ws.connect('message', (_ws, _type, bytes) => {
                    try {
                        const text = new TextDecoder().decode(bytes.get_data());
                        const data = JSON.parse(text);
                        if (data?.error === 'unauthorized') {
                            this._markAuthFailed();
                            return;
                        }
                        if (data?.type === 'config') return;
                        this._handleMessage(data);
                    } catch (_) {}
                });

                ws.connect('closed', () => {
                    const code = ws.get_close_code();
                    this._ws = null;
                    if (code === 1008) {
                        this._markAuthFailed();
                        return;
                    }
                    if (!this._authFailed) {
                        this._setOffline();
                        this._scheduleReconnect();
                    }
                });

                ws.connect('error', () => {
                    this._ws = null;
                    if (!this._authFailed) {
                        this._setOffline();
                        this._scheduleReconnect();
                    }
                });
            }
        );
    }

    _markAuthFailed() {
        if (this._authFailed)
            return;

        this._authFailed = true;
        this._setAuthFailed();
        if (this._ws) {
            this._ws.close(1000, null);
            this._ws = null;
        }
        Main.notify('codelight', 'Wrong password. Open Settings to fix.');
    }

    _handleMessage(data) {
        const status   = data?.status ?? 'offline';
        const color    = C[status] ?? C.offline;
        const hex      = toHex(color);
        const sessions = data?.sessions ?? 0;
        const label    = status.toUpperCase();

        this._panelDot.set_style(`color: ${hex};`);
        this._panelDot.set_text('● ');
        this._panelStatus.set_text(label);

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
        this._panelDot.set_style(`color: ${hex};`);
        this._panelDot.set_text('● ');
        this._panelStatus.set_text('OFFLINE');
        this._hdrDot.set_style(`color: ${hex};`);
        this._hdrDot.set_text('●');
        this._hdrStatus.set_text('OFFLINE');
        this._hdrSessions.set_text('daemon offline');
        this._setMeter(this._weeklyItem,  null, null);
        this._setMeter(this._sessionItem, null, null);
    }

    _setAuthFailed() {
        const hex = toHex(C.waiting);
        this._panelDot.set_style(`color: ${hex};`);
        this._panelDot.set_text('● ');
        this._panelStatus.set_text('AUTH FAIL');
        this._hdrDot.set_style(`color: ${hex};`);
        this._hdrDot.set_text('●');
        this._hdrStatus.set_text('AUTH FAIL');
        this._hdrSessions.set_text('wrong password');
        this._setMeter(this._weeklyItem,  null, null);
        this._setMeter(this._sessionItem, null, null);
    }

    _disconnect() {
        if (this._reconnectTimer !== null) {
            GLib.source_remove(this._reconnectTimer);
            this._reconnectTimer = null;
        }
        if (this._ws) {
            this._ws.close(1000, null);
            this._ws = null;
        }
        this._setOffline();
    }

    _scheduleReconnect(delay = RECONNECT_DELAY_S) {
        if (this._reconnectTimer !== null) return;
        this._reconnectTimer = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, delay, () => {
                this._reconnectTimer = null;
                this._connect();
                return GLib.SOURCE_REMOVE;
            }
        );
    }

    disable() {
        if (this._settingsChangedId) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = null;
        }
        this._disconnect();
        this._session?.abort();
        this._session  = null;
        this._settings = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
