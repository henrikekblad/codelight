import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Pango from 'gi://Pango';
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
    <signal name="QuestionRequest">
      <arg type="s" name="request_json"/>
    </signal>
    <signal name="QuestionResolved">
      <arg type="s" name="resolved_json"/>
    </signal>
    <method name="RespondQuestion">
      <arg direction="in" type="s" name="request_id"/>
      <arg direction="in" type="s" name="answers_json"/>
      <arg direction="out" type="b"/>
    </method>
    <method name="ExtendRequest">
      <arg direction="in" type="s" name="request_id"/>
      <arg direction="out" type="b"/>
    </method>
    <method name="Announce">
      <arg direction="in" type="s" name="features_json"/>
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

function agentName(data) {
    const raw = String(data?.agent_display ?? data?.agent_id ?? 'Claude').trim();
    if (!raw) return 'Claude';
    return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function agentIconKey(data) {
    const raw = String(data?.agent_id ?? '').trim().toLowerCase();
    return raw || 'claude';
}

function normalizedStatus(status) {
    const value = String(status || 'idle').trim().toLowerCase();
    return value === 'inactive' ? 'idle' : value;
}

function usageLimits(usage) {
    if (Array.isArray(usage?.limits)) return usage.limits.slice(0, 2);
    return [];
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
    item.set_style('padding: 2px 12px 3px;');

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

    const bar = new St.DrawingArea({ height: 7, x_expand: true, style: 'margin-top: 3px;' });
    bar._pct = 0;
    bar.connect('repaint', a => drawBar(a, a._pct, usageColor(a._pct), C.barBg));

    col.add_child(row);
    col.add_child(bar);
    item.add_child(col);

    item._pctLabel = pct;
    item._rstLabel = rst;
    item._bar      = bar;
    item._label    = lbl;
    return item;
}

function makeAgentHeader(name, separated = false) {
    const item = new PopupMenu.PopupBaseMenuItem({ reactive: false });
    item.set_style(separated ? 'padding: 12px 12px 3px;' : 'padding: 4px 12px 3px;');
    const row = new St.BoxLayout({
        x_expand: true,
        style: 'padding-bottom: 3px; border-bottom: 1px solid #555555;',
    });
    const label = new St.Label({
        text: name,
        style: 'color: #eeeeee; font-size: 12px; font-weight: bold;',
        x_expand: true,
    });
    const status = new St.Label({
        text: 'IDLE',
        style: 'color: #888888; font-size: 10px; font-weight: bold;',
        x_align: Clutter.ActorAlign.END,
        y_align: Clutter.ActorAlign.CENTER,
    });
    row.add_child(label);
    row.add_child(status);
    item.add_child(row);
    item._nameLabel = label;
    item._statusLabel = status;
    return item;
}

// St.Label doesn't wrap by default — long question text gets clipped/ellipsized.
function wrapLabel(text, style) {
    const props = { text: text || '', x_expand: true };
    if (style) props.style = style;   // St throws on style: undefined
    const l = new St.Label(props);
    l.clutter_text.line_wrap = true;
    l.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    l.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    return l;
}

function _extractPatchTarget(patchText) {
    for (const raw of String(patchText || '').split('\n')) {
        const line = raw.trim();
        if (line.startsWith('*** Update File:')) return line.slice('*** Update File:'.length).trim();
        if (line.startsWith('*** Add File:')) return line.slice('*** Add File:'.length).trim();
        if (line.startsWith('*** Delete File:')) return line.slice('*** Delete File:'.length).trim();
    }
    return '';
}

function _formatPermissionBody(req) {
    const tool = String(req?.tool_name || req?.toolName || 'tool use').trim() || 'tool use';
    const summary = String(req?.summary || '').trim();
    const ti = (req && typeof req.tool_input === 'object' && req.tool_input) ? req.tool_input : null;

    if (ti) {
        const plan = String(ti.plan || '').trim();
        if (plan) return plan;

        const command = String(ti.command || '').trim();
        if (command) return command;

        const filePath = String(ti.file_path || ti.filePath || '').trim();
        if (filePath) return filePath;

        if (tool === 'apply_patch') {
            const explanation = String(ti.explanation || '').trim();
            const target = _extractPatchTarget(ti.input || '');
            const parts = [];
            if (explanation) parts.push(explanation);
            if (target) parts.push(`target=${target}`);
            if (parts.length) return parts.join('\n');
        }

        if (tool === 'run_in_terminal') {
            const goal = String(ti.goal || '').trim();
            const explanation = String(ti.explanation || '').trim();
            if (goal || explanation) return [goal, explanation].filter(Boolean).join('\n');
        }
    }

    // Try to strip "Tool: ..." prefix and pretty-print JSON-ish payloads.
    let detail = summary;
    const m = summary.match(/^[^:]{1,80}:\s*(.*)$/);
    if (m && m[1]) detail = m[1].trim();

    const looksJson = (detail.startsWith('{') && detail.endsWith('}')) ||
                      (detail.startsWith('[') && detail.endsWith(']'));
    if (looksJson) {
        try { return JSON.stringify(JSON.parse(detail), null, 2); }
        catch (_) {}
    }

    return detail || tool;
}

export default class CodelightExtension extends Extension {
    enable() {
        this._proxy    = null;
        this._signalId = null;
        this._watchId  = null;
        this._settings = this.getSettings();
        // Re-announce presence/features when the prompt toggles change.
        this._settingsIds = [
            this._settings.connect('changed::permission-prompts', () => this._announce()),
            this._settings.connect('changed::question-prompts',   () => this._announce()),
        ];
        this._permSignalIds = [];
        this._reqActiveId  = null;         // request id shown in the panel popup
        this._reqKind      = null;         // 'permission' | 'question'
        this._reqQueue     = [];           // requests waiting behind the active one
        this._reqFinishing = false;        // guard so our own menu.close() isn't read as a dismiss
        this._qState       = null;         // [{selected:Set, multi, entry}] for an active question
        this._qQuestions   = null;
        this._qKeepalive   = null;         // timer id: extends the daemon deadline while open
        this._announceTimer = null;        // timer id: presence heartbeat to the daemon
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

        // Meter rows. Keep both desktop agents visible; compact clients can
        // continue using the active-agent compatibility fields.
        this._usageItems = {};
        for (const [index, [agent, display]] of
            [['claude', 'Claude'], ['copilot', 'Copilot'], ['codex', 'Codex']].entries()) {
            const header = makeAgentHeader(display, index > 0);
            const weekly = makeMeterItem('Weekly');
            const session = makeMeterItem('Session');
            const meters = [weekly, session];
            this._usageItems[agent] = {header, weekly, session, meters};
            this._indicator.menu.addMenuItem(header);
            this._indicator.menu.addMenuItem(weekly);
            this._indicator.menu.addMenuItem(session);
        }

        // Status/limits rows — hidden while a request is being answered so the
        // popup shows only the question/permission.
        this._statusItems = [
            hdrItem,
            ...Object.values(this._usageItems)
                .flatMap(items => [items.header, items.weekly, items.session]),
        ];

        // Question section (populated when Claude asks; empty otherwise)
        this._qSection = new PopupMenu.PopupMenuSection();
        this._indicator.menu.addMenuItem(this._qSection);
        // Closing the popup does NOT discard a pending request — the section
        // stays built so reopening the icon shows it again. Keepalive only runs
        // while the popup is open; once closed, the daemon idle-times-out after
        // ~60 s (falling through to Claude's own prompt) unless reopened.
        this._indicator.menu.connect('open-state-changed', (_m, open) => {
            if (!this._reqActiveId) return;
            if (open) this._startKeepalive();
            else this._stopKeepalive();
        });

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
            this._permSignalIds.push(this._proxy.connectSignal('QuestionRequest',
                (_proxy, _sender, [json]) => this._onQuestionRequest(json)));
            this._permSignalIds.push(this._proxy.connectSignal('QuestionResolved',
                (_proxy, _sender, [json]) => this._onQuestionResolved(json)));
            // Fetch current state immediately so the panel isn't blank on connect
            try {
                const result = this._proxy.call_sync('GetStatus', null, Gio.DBusCallFlags.NONE, -1, null);
                const [json] = result.deepUnpack();
                const data = JSON.parse(json);
                if (data?.type !== 'config') this._handleMessage(data);
            } catch (_) {}
            // Announce presence now + on a heartbeat so the daemon knows this
            // extension can answer, and won't fall AskUserQuestion through to the
            // local dialog while we're listening.
            this._announce();
            this._startAnnounce();
        } catch (e) {
            logError(e, 'codelight D-Bus connect failed');
        }
    }

    _announce() {
        if (this._proxy === null || !this._settings) return;
        const features = [];
        if (this._settings.get_boolean('permission-prompts')) features.push('permissions');
        if (this._settings.get_boolean('question-prompts'))   features.push('questions');
        try {
            this._proxy.call_sync('Announce',
                new GLib.Variant('(s)', [JSON.stringify(features)]),
                Gio.DBusCallFlags.NONE, -1, null);
        } catch (_) {}
    }

    _startAnnounce() {
        this._stopAnnounce();
        // Shorter than the daemon's GNOME_PRESENCE_TTL (90 s) so presence never lapses.
        this._announceTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 40, () => {
            this._announce();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopAnnounce() {
        if (this._announceTimer) {
            GLib.source_remove(this._announceTimer);
            this._announceTimer = null;
        }
    }

    _onDaemonVanished() {
        this._disconnectProxy();
        this._clearAllRequests();
        this._setOffline();
    }

    _disconnectProxy() {
        this._stopAnnounce();
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

    // ── Remote requests (permission + question) in the panel popup ────────────
    // Both surfaces share one section, opened from the panel icon (no focus
    // grab). One request at a time; others queue behind it.

    _onPermissionRequest(json) {
        let req;
        try { req = JSON.parse(json); } catch (_) { return; }
        if (!req?.id) return;
        if (!this._settings.get_boolean('permission-prompts')) return;
        this._enqueueRequest({ ...req, kind: 'permission' });
    }

    _onQuestionRequest(json) {
        let req;
        try { req = JSON.parse(json); } catch (_) { return; }
        if (!req?.id) return;
        if (!this._settings.get_boolean('question-prompts')) return;
        this._enqueueRequest({ ...req, kind: 'question' });
    }

    _enqueueRequest(req) {
        if (this._reqActiveId === req.id || this._reqQueue.some(r => r.id === req.id)) return;
        if (this._reqActiveId) { this._reqQueue.push(req); return; }   // one at a time
        this._showRequest(req);
    }

    _showRequest(req) {
        this._reqActiveId = req.id;
        this._reqKind = req.kind;
        this._qState = [];
        this._qQuestions = req.questions || [];
        this._qSection.removeAll();
        this._statusItems?.forEach(i => { i.visible = false; });   // hide limits/status

        const head = new PopupMenu.PopupBaseMenuItem({ reactive: false });
        head.add_child(new St.Label({
            text: req.kind === 'permission'
                ? `${agentName(req)} requests permission`
                : `${agentName(req)} asks`,
            style: 'font-weight: bold; color: #eeeeee;' }));
        this._qSection.addMenuItem(head);

        if (req.kind === 'permission')
            this._buildPermission(req);
        else
            this._buildQuestion(req);

        this._indicator.menu.open(true);
        this._startKeepalive();   // also (re)started by open-state-changed
    }

    // Keepalive while the popup is open: push the daemon deadline out every 20 s
    // (< the 60 s idle timeout) so it never times out mid-interaction.
    _startKeepalive() {
        this._stopKeepalive();
        const id = this._reqActiveId;
        if (!id) return;
        this._qKeepalive = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 20, () => {
            this._proxy?.call('ExtendRequest',
                new GLib.Variant('(s)', [id]), Gio.DBusCallFlags.NONE, -1, null, null);
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopKeepalive() {
        if (this._qKeepalive) { GLib.source_remove(this._qKeepalive); this._qKeepalive = null; }
    }

    _buildPermission(req) {
        const item = new PopupMenu.PopupBaseMenuItem({ reactive: false });
        const box  = new St.BoxLayout({ vertical: true, x_expand: true, style: 'spacing: 8px; width: 380px;' });
        const canAllowFolder = req?.allow_folder_available !== false;
        const canAllowCommand = req?.allow_command_available === true;
        const tool = String(req.tool_name || req.toolName || 'tool use');
        const body = _formatPermissionBody(req);
        box.add_child(wrapLabel(`Allow ${tool}?`, 'font-size: 12px; color: #bbbbbb;'));
        box.add_child(wrapLabel(body,
            'font-family: monospace; font-size: 11px; color: #c8c8c8;'));

        const row = new St.BoxLayout({ x_expand: true, style: 'spacing: 8px; padding-top: 4px;' });
        const allow = new St.Button({ x_expand: true, style: 'padding: 6px; border-radius: 6px; background-color: #238636; color: #fff;', child: new St.Label({ text: 'Allow' }) });
        const deny  = new St.Button({ x_expand: true, style: 'padding: 6px; border-radius: 6px; background-color: #6e2b2b; color: #fff;', child: new St.Label({ text: 'Deny' }) });
        allow.connect('clicked', () => this._finishRequest({ decision: 'allow' }));
        deny.connect('clicked',  () => this._finishRequest({ decision: 'deny' }));
        row.add_child(allow);
        row.add_child(deny);
        box.add_child(row);

        if (canAllowFolder) {
            box.add_child(wrapLabel(
                'Trusting this folder auto-allows read-only inspection and safe, non-delete patches inside it.',
                'font-size: 11px; color: #ffb74d;'
            ));

            const trustRow = new St.BoxLayout({ x_expand: true, style: 'padding-top: 2px;' });
            const allowFolder = new St.Button({
                x_expand: true,
                style: 'padding: 6px; border-radius: 6px; background-color: #2f6f9f; color: #fff;',
                child: new St.Label({ text: 'Allow + Trust Folder for Safe Edits' }),
            });
            allowFolder.connect('clicked', () => this._finishRequest({ decision: 'allow_folder' }));
            trustRow.add_child(allowFolder);
            box.add_child(trustRow);
        }

        if (canAllowCommand) {
            box.add_child(wrapLabel(
                'Allow this exact command automatically in this repository in future sessions and agents.',
                'font-size: 11px; color: #ffb74d;'
            ));
            const commandRow = new St.BoxLayout({ x_expand: true, style: 'padding-top: 2px;' });
            const allowCommand = new St.Button({
                x_expand: true,
                style: 'padding: 6px; border-radius: 6px; background-color: #2f6f9f; color: #fff;',
                child: new St.Label({ text: 'Allow + Always Allow Exact Command Here' }),
            });
            allowCommand.connect('clicked',
                () => this._finishRequest({ decision: 'allow_command' }));
            commandRow.add_child(allowCommand);
            box.add_child(commandRow);
        }

        item.add_child(box);
        this._qSection.addMenuItem(item);
    }

    _buildQuestion(req) {
        (req.questions || []).forEach((q) => {
            const multi = !!q.multiSelect;
            const st = { selected: new Set(), multi, entry: null, buttons: [] };
            this._qState.push(st);

            const qItem = new PopupMenu.PopupBaseMenuItem({ reactive: false });
            const qBox  = new St.BoxLayout({ vertical: true, x_expand: true, style: 'spacing: 4px; max-width: 360px;' });
            if (q.header) qBox.add_child(wrapLabel(q.header, 'font-size: 10px; color: #888888;'));
            qBox.add_child(wrapLabel(q.question, 'font-size: 12px;'));

            (q.options || []).forEach((opt) => {
                const label = opt.label ?? String(opt);
                const btn = new St.Button({ x_expand: true,
                    child: wrapLabel(opt.description ? `${label} — ${opt.description}` : label) });
                const setSel = (on) => btn.set_style(
                    'padding: 6px 8px; border-radius: 6px; text-align: left; border: 1px solid ' +
                    (on ? '#00C800; background-color: rgba(0,200,0,0.15);' : '#444;'));
                setSel(false);
                btn.connect('clicked', () => {
                    if (multi) {
                        if (st.selected.has(label)) { st.selected.delete(label); setSel(false); }
                        else { st.selected.add(label); setSel(true); }
                    } else {
                        st.selected.clear();
                        st.buttons.forEach(([, s]) => s(false));
                        st.selected.add(label); setSel(true);
                    }
                });
                st.buttons.push([label, setSel]);
                qBox.add_child(btn);
            });

            st.entry = new St.Entry({ hint_text: 'Other…', can_focus: true, style: 'margin-top: 4px;' });
            qBox.add_child(st.entry);
            qItem.add_child(qBox);
            this._qSection.addMenuItem(qItem);
        });

        const btnItem = new PopupMenu.PopupBaseMenuItem({ reactive: false });
        const btnBox  = new St.BoxLayout({ x_expand: true, style: 'spacing: 8px; padding-top: 6px;' });
        const submit  = new St.Button({ x_expand: true, style: 'padding: 6px; border-radius: 6px; background-color: #238636; color: #fff;', child: new St.Label({ text: 'Submit' }) });
        const skip    = new St.Button({ style: 'padding: 6px 10px; border-radius: 6px; border: 1px solid #444;', child: new St.Label({ text: 'Skip' }) });
        submit.connect('clicked', () => this._submitQuestion());
        skip.connect('clicked', () => this._finishRequest({ skip: true }));
        btnBox.add_child(submit);
        btnBox.add_child(skip);
        btnItem.add_child(btnBox);
        this._qSection.addMenuItem(btnItem);
    }

    _submitQuestion() {
        const answers = {};
        for (let i = 0; i < this._qState.length; i++) {
            const st = this._qState[i];
            const parts = [...st.selected];
            const other = st.entry?.get_text()?.trim();
            if (other) parts.push(other);
            if (!parts.length) return;   // unanswered → keep popup open
            answers[this._qQuestions[i].question] = parts.join(', ');
        }
        this._finishRequest({ answers });
    }

    // opts: {decision} for permission, {answers}|{skip} for question,
    // {} for a plain dismiss (leave pending → the hook times out to local UI).
    _finishRequest(opts = {}) {
        const id = this._reqActiveId;
        const kind = this._reqKind;
        if (!id) return;
        this._reqFinishing = true;
        try {
            if (kind === 'permission' && opts.decision) {
                this._proxy?.call_sync('RespondPermission',
                    new GLib.Variant('(ss)', [id, opts.decision]),
                    Gio.DBusCallFlags.NONE, -1, null);
            } else if (kind === 'question') {
                // answers dict → answer; skip/dismiss → "{}" → daemon falls through
                this._proxy?.call_sync('RespondQuestion',
                    new GLib.Variant('(ss)', [id, JSON.stringify(opts.answers || {})]),
                    Gio.DBusCallFlags.NONE, -1, null);
            }
            // permission dismiss (no decision): send nothing → hook times out → local prompt
        } catch (e) {
            logError(e, 'codelight respond failed');
        }
        this._clearRequest();
        this._indicator.menu.close();
        this._reqFinishing = false;
        const next = this._reqQueue.shift();
        if (next) this._showRequest(next);
    }

    _onPermissionResolved(json) { this._onResolved(json); }
    _onQuestionResolved(json)   { this._onResolved(json); }

    _onResolved(json) {
        let id;
        try { id = JSON.parse(json)?.id; } catch (_) { return; }
        if (id && id === this._reqActiveId) {
            this._clearRequest();
            this._indicator.menu.close();
            const next = this._reqQueue.shift();
            if (next) this._showRequest(next);
        } else if (id) {
            this._reqQueue = this._reqQueue.filter(r => r.id !== id);
        }
    }

    _clearRequest() {
        this._stopKeepalive();
        this._reqActiveId = null;
        this._reqKind = null;
        this._qState = null;
        this._qQuestions = null;
        this._qSection?.removeAll();
        this._statusItems?.forEach(i => { i.visible = true; });   // restore limits/status
    }

    _clearAllRequests() {
        this._reqQueue = [];
        this._clearRequest();
    }

    _handleMessage(data) {
        // Don't update meters while a permission/question request is active.
        // Meter updates can visually leak into/behind the request popup.
        const hasActiveRequest = this._reqActiveId !== null;

        const status = normalizedStatus(data?.status ?? 'offline');
        const color    = C[status] ?? C.offline;
        const hex      = toHex(color);
        const sessions = data?.sessions ?? 0;
        const activeName = agentName(data);

        // statuses without an icon of their own fall back to the offline icon
        const agent = agentIconKey(data).replace(/[^a-z0-9_-]/g, '');
        const iconStatus = C[status] ? status : 'offline';
        const preferred = `${this.path}/icons/${agent}-${iconStatus}.svg`;
        const fallback = `${this.path}/icons/claude-${iconStatus}.svg`;
        const iconPath = GLib.file_test(preferred, GLib.FileTest.EXISTS) ? preferred : fallback;
        this._panelIcon.gicon = Gio.icon_new_for_string(iconPath);

        this._hdrDot.set_style(`color: ${hex};`);
        this._hdrDot.set_text('●');
        this._hdrStatus.set_text(`${activeName} ${status.toUpperCase()}`);
        this._hdrSessions.set_text(sessions === 1 ? '1 session' : `${sessions} sessions`);

        if (!hasActiveRequest) {
            const perAgent = data?.per_agent_usage;
            const perAgentStatus = data?.per_agent_status;
            if (perAgent && typeof perAgent === 'object') {
                for (const [agentId, items] of Object.entries(this._usageItems)) {
                    const usage = perAgent[agentId];
                    const hasStatus = perAgentStatus && Object.prototype.hasOwnProperty.call(perAgentStatus, agentId);
                    const display = usage?.agent_display ??
                        agentId.charAt(0).toUpperCase() + agentId.slice(1);
                    items.header.visible = !!usage || !!hasStatus;
                    items.header._nameLabel.set_text(display);
                    items.header._statusLabel.set_text(
                        normalizedStatus(perAgentStatus?.[agentId]).toUpperCase());
                    const limits = usageLimits(usage);
                    items.meters.forEach((meter, index) => {
                        const limit = limits[index];
                        meter.visible = !!limit;
                        if (limit)
                            this._setMeter(meter, limit.pct, limit.reset, limit.label);
                    });
                }
            } else {
                const items = this._usageItems[agent] ?? this._usageItems.claude;
                for (const [agentId, candidate] of Object.entries(this._usageItems)) {
                    candidate.header.visible = agentId === agent;
                    candidate.weekly.visible = agentId === agent;
                    candidate.session.visible = agentId === agent;
                }
                items.header._nameLabel.set_text(activeName);
                items.header._statusLabel.set_text(status.toUpperCase());
                this._setMeter(items.weekly, data?.weekly_pct, data?.weekly_reset, 'Weekly');
                this._setMeter(items.session, data?.session_pct, data?.session_reset, 'Session');
            }
        }
    }

    _setMeter(item, pct, reset, title = null) {
        if (title) item._label.set_text(`${title} — `);
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
        for (const [agent, items] of Object.entries(this._usageItems)) {
            items.header.visible = agent === 'claude';
            items.weekly.visible = false;
            items.session.visible = false;
            items.header._statusLabel.set_text('OFFLINE');
            this._setMeter(items.weekly, null, null);
            this._setMeter(items.session, null, null);
        }
    }

    disable() {
        if (this._watchId !== null) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = null;
        }
        this._disconnectProxy();
        this._clearAllRequests();
        if (this._settings && this._settingsIds) {
            for (const id of this._settingsIds) this._settings.disconnect(id);
        }
        this._settingsIds = null;
        this._settings = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
