import * as vscode from 'vscode';
import { CodelightClient } from './client';

let client: CodelightClient | undefined;
let statusItem: vscode.StatusBarItem;

const STATUS_ICON: Record<string, string> = {
    working: '$(sync~spin)',
    waiting: '$(bell-dot)',
    idle:    '$(check)',
};

// ── Question state ────────────────────────────────────────────────────────────
let pending: any | undefined;                 // the active question_request
let panel: vscode.WebviewPanel | undefined;   // the open question WebView
let keepalive: NodeJS.Timeout | undefined;
let lastStatus: any | undefined;              // last status payload, to restore the bar

function agentName(payload: any): string {
    const raw = String(payload?.agent_display ?? payload?.agent_id ?? 'Claude').trim();
    if (!raw) { return 'Claude'; }
    return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function availableUsage(p: any): Array<[string, any]> {
    const perAgent = p?.per_agent_usage;
    const perStatus = p?.per_agent_status;
    if ((perAgent && typeof perAgent === 'object') ||
        (perStatus && typeof perStatus === 'object')) {
        return ['claude', 'copilot', 'codex']
            .filter(id => (perAgent?.[id] && typeof perAgent[id] === 'object') ||
                Object.prototype.hasOwnProperty.call(perStatus ?? {}, id))
            .map(id => [id, perAgent?.[id]]);
    }
    return [[String(p?.agent_id ?? 'claude'), p]];
}

function usageLimits(value: any): Array<{label: string; pct: number; reset: string}> {
    if (Array.isArray(value?.limits)) {
        return value.limits.map((limit: any) => ({
            label: String(limit?.label ?? 'Limit'),
            pct: Number(limit?.pct ?? 0),
            reset: String(limit?.reset ?? '--'),
        }));
    }
    if (!value) { return []; }
    return [
        { label: 'Weekly', pct: value.weekly_pct ?? 0, reset: value.weekly_reset ?? '--' },
        { label: 'Session', pct: value.session_pct ?? 0, reset: value.session_reset ?? '--' },
    ];
}

function normalizedStatus(value: unknown): string {
    const status = String(value ?? 'idle').trim().toLowerCase();
    return status === 'inactive' ? 'idle' : status;
}

function applyStatus(p: any): void {
    const status = normalizedStatus(p?.status);
    const icon = STATUS_ICON[status] ?? '$(circle-outline)';
    const usage = availableUsage(p);
    const perAgentStatus = p?.per_agent_status;
    const details = usage.flatMap(([id, value], index) => {
        const name = String(value?.agent_display ??
            (id.charAt(0).toUpperCase() + id.slice(1)));
        const agentStatus = normalizedStatus(perAgentStatus?.[id] ??
            (id === p?.agent_id ? status : 'idle')).toUpperCase();
        return [
            `${index ? '\n' : ''}${name} — ${agentStatus}`,
            ...usageLimits(value).map(limit =>
                `  ${limit.label} ${Math.round(limit.pct * 100)}% ` +
                `(resets ${limit.reset})`),
        ];
    });
    statusItem.text = `${icon} ${agentName(p)} ${status.toUpperCase()}`;
    statusItem.tooltip = [
        'codelight',
        ...details,
    ].join('\n');
    statusItem.command = undefined;
    statusItem.show();
}

function showPromptStatus(req: any): void {
    const name = agentName(req);
    const isPermission = req?.type === 'permission_request';
    const summary = isPermission
        ? (req?.summary ?? `${name} wants permission to run a tool`)
        : (req?.questions?.[0]?.question ?? `${name} has a question`);
    statusItem.text = isPermission
        ? `$(shield) ${name} PERMISSION`
        : `$(bell-dot) ${name} QUESTION`;
    statusItem.tooltip = `codelight — ${summary}\n(click to review)`;
    statusItem.command = 'codelight.answerQuestion';
    statusItem.show();
}

function startKeepalive(id: string): void {
    stopKeepalive();
    keepalive = setInterval(() => client?.extend(id), 20_000);
}
function stopKeepalive(): void {
    if (keepalive) { clearInterval(keepalive); keepalive = undefined; }
}

function disposePanel(): void {
    const p = panel;
    panel = undefined;
    p?.dispose();
}

function clearPending(): void {
    pending = undefined;
    stopKeepalive();
    if (lastStatus) { applyStatus(lastStatus); } else { statusItem.command = undefined; }
}

/** Open (or reveal) the WebView prompt for a question_request. */
function showRequest(req: any): void {
    // A fresh panel per request avoids stale/blank content and keeps message
    // handlers bound to the active request id.
    if (panel) { disposePanel(); }

    panel = vscode.window.createWebviewPanel(
        'codelightQuestion',
        req?.type === 'permission_request'
            ? `${agentName(req)} requests permission`
            : `${agentName(req)} asks`,
        { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
        { enableScripts: true, retainContextWhenHidden: true },
    );
    try {
        panel.webview.html = renderHtml(panel.webview, req);
    } catch (e) {
        panel.webview.html = renderFallbackHtml(panel.webview, req, String(e));
    }

    panel.webview.onDidReceiveMessage((msg) => {
        if (!pending || pending.id !== req.id) { return; }
        if (msg?.type === 'submit' && req?.type === 'question_request') {
            client?.respondQuestion(req.id, msg.answers ?? {});
            clearPending();
            disposePanel();
        } else if (msg?.type === 'skip' && req?.type === 'question_request') {
            client?.respondQuestion(req.id, {});   // empty → Claude's local dialog
            clearPending();
            disposePanel();
        } else if (msg?.type === 'allow' && req?.type === 'permission_request') {
            client?.respondPermission(req.id, 'allow');
            clearPending();
            disposePanel();
        } else if (msg?.type === 'allow_folder' && req?.type === 'permission_request') {
            client?.respondPermission(req.id, 'allow_folder');
            clearPending();
            disposePanel();
        } else if (msg?.type === 'allow_command' && req?.type === 'permission_request') {
            client?.respondPermission(req.id, 'allow_command');
            clearPending();
            disposePanel();
        } else if (msg?.type === 'deny' && req?.type === 'permission_request') {
            client?.respondPermission(req.id, 'deny');
            clearPending();
            disposePanel();
        } else if (msg?.type === 'fallback' && req?.type === 'permission_request') {
            client?.respondPermission(req.id, 'skip');
            clearPending();
            disposePanel();
        }
    });

    // Closing the tab without answering keeps the request pending (reopen via the
    // status bar); keepalive stops so the daemon idle-times-out if it's left.
    panel.onDidDispose(() => { panel = undefined; stopKeepalive(); });

    startKeepalive(req.id);
}

async function activateAndShow(req: any): Promise<void> {
    pending = req;
    showPromptStatus(req);
    showRequest(req);
}

// ── WebView HTML ──────────────────────────────────────────────────────────────

function esc(s: string): string {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function getNonce(): string {
    let t = '';
    const c = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) { t += c.charAt(Math.floor(Math.random() * c.length)); }
    return t;
}

function renderHtml(webview: vscode.Webview, req: any): string {
    if (req?.type === 'permission_request') {
        return renderPermissionHtml(webview, req);
    }

    const nonce = getNonce();
    const questions = Array.isArray(req?.questions) ? req.questions : [];

    const blocks = questions.map((q: any, i: number) => {
        const multi = !!q.multiSelect;
        const opts = Array.isArray(q.options) ? q.options : [];
        const rows = opts.map((o: any) => `
            <label class="opt">
                <input type="${multi ? 'checkbox' : 'radio'}" name="q${i}" value="${esc(o.label)}">
                <span class="opt-text"><span class="opt-label">${esc(o.label)}</span>${
                    o.description ? `<span class="opt-desc">${esc(o.description)}</span>` : ''
                }</span>
            </label>`).join('');
        return `
        <section class="q" data-question="${esc(q.question)}">
            ${q.header ? `<div class="hdr">${esc(q.header)}</div>` : ''}
            <div class="qtext">${esc(q.question)}</div>
            <div class="opts">${rows}</div>
            <input type="text" class="other" data-q="${i}" placeholder="Other… (type a custom answer)">
        </section>`;
    }).join('');

    const fallback = questions.length === 0
        ? `<section class="q"><div class="qtext">No question payload found.</div>
           <div class="opts"><pre>${esc(JSON.stringify(req ?? {}, null, 2))}</pre></div></section>`
        : '';

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
    body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
           background: var(--vscode-editor-background); padding: 16px 18px; }
    h1 { font-size: 15px; font-weight: 600; margin: 0 0 14px; }
    .q { margin-bottom: 20px; padding-bottom: 4px; }
    .q.missing { outline: 1px solid var(--vscode-inputValidation-errorBorder, #be1100);
                 outline-offset: 6px; border-radius: 4px; }
    .hdr { font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
           color: var(--vscode-descriptionForeground); margin-bottom: 3px; }
    .qtext { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
    .opt { display: flex; align-items: flex-start; gap: 8px; padding: 6px 8px; border-radius: 5px;
           cursor: pointer; border: 1px solid transparent; }
    .opt:hover { background: var(--vscode-list-hoverBackground); }
    .opt input { margin-top: 3px; accent-color: var(--vscode-focusBorder); }
    .opt-text { display: flex; flex-direction: column; }
    .opt-label { font-size: 13px; }
    .opt-desc { font-size: 12px; color: var(--vscode-descriptionForeground); }
    .other { width: 100%; box-sizing: border-box; margin-top: 8px; padding: 6px 8px;
             color: var(--vscode-input-foreground); background: var(--vscode-input-background);
             border: 1px solid var(--vscode-input-border, transparent); border-radius: 4px; }
    .other:focus { outline: 1px solid var(--vscode-focusBorder); }
    .actions { display: flex; gap: 10px; margin-top: 8px; }
    button { font-family: inherit; font-size: 13px; padding: 6px 14px; border: none;
             border-radius: 4px; cursor: pointer; }
    .primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
    .primary:hover { background: var(--vscode-button-hoverBackground); }
    .secondary { background: var(--vscode-button-secondaryBackground);
                 color: var(--vscode-button-secondaryForeground); }
    .secondary:hover { background: var(--vscode-button-secondaryHoverBackground); }
    .err { color: var(--vscode-inputValidation-errorForeground, #f48771); font-size: 12px;
           min-height: 16px; margin-bottom: 8px; }
</style>
</head>
<body>
    <h1>${esc(agentName(req))} asks</h1>
    ${blocks || fallback}
    <div id="err" class="err"></div>
    <div class="actions">
        <button id="submit" class="primary">Submit</button>
        <button id="skip" class="secondary">Skip — use ${esc(agentName(req))}'s own prompt</button>
    </div>
<script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.getElementById('submit').addEventListener('click', () => {
        const answers = {};
        let missing = false;
        document.querySelectorAll('.q').forEach((qEl) => {
            const question = qEl.getAttribute('data-question');
            const checked = Array.from(qEl.querySelectorAll('input[type=checkbox]:checked, input[type=radio]:checked')).map(e => e.value);
            const other = qEl.querySelector('.other').value.trim();
            const parts = checked.slice();
            if (other) { parts.push(other); }
            if (!parts.length) { missing = true; qEl.classList.add('missing'); }
            else { qEl.classList.remove('missing'); }
            answers[question] = parts.join(', ');
        });
        if (missing) {
            document.getElementById('err').textContent =
                'Please answer every question — pick an option or type an answer.';
            return;
        }
        vscode.postMessage({ type: 'submit', answers });
    });
    document.getElementById('skip').addEventListener('click', () => vscode.postMessage({ type: 'skip' }));
</script>
</body>
</html>`;
}

function renderFallbackHtml(webview: vscode.Webview, req: any, err: string): string {
    const nonce = getNonce();
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
    body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
           background: var(--vscode-editor-background); padding: 16px 18px; }
    h1 { font-size: 15px; font-weight: 600; margin: 0 0 10px; }
    p { color: var(--vscode-descriptionForeground); }
    pre { white-space: pre-wrap; word-break: break-word; font-family: var(--vscode-editor-font-family, monospace);
          font-size: 12px; padding: 12px; border-radius: 6px; overflow: auto;
          background: var(--vscode-textCodeBlock-background, rgba(128,128,128,0.12));
          border: 1px solid var(--vscode-widget-border, transparent); }
</style>
</head>
<body>
    <h1>codelight request panel failed to render</h1>
    <p>The request is still pending. Details are shown below for debugging.</p>
    <pre>render error: ${esc(err)}</pre>
    <pre>${esc(JSON.stringify(req ?? {}, null, 2))}</pre>
</body>
</html>`;
}

function renderResolvedElsewhereHtml(webview: vscode.Webview, by: string, req: any): string {
    const nonce = getNonce();
    const actor = esc(by || 'another client');
    const kind = req?.type === 'permission_request' ? 'permission request' : 'question';
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
    body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
           background: var(--vscode-editor-background); padding: 16px 18px; }
    h1 { font-size: 15px; font-weight: 600; margin: 0 0 8px; }
    p { color: var(--vscode-descriptionForeground); margin: 0 0 8px; }
</style>
</head>
<body>
    <h1>Request already answered</h1>
    <p>This ${esc(kind)} was answered by ${actor}.</p>
    <p>You can continue in the chat; no action is needed in this panel.</p>
</body>
</html>`;
}

function renderPermissionHtml(webview: vscode.Webview, req: any): string {
    const nonce = getNonce();
    const name = agentName(req);
    const toolName = esc(req?.tool_name ?? '?');
    const canAllowFolder = req?.allow_folder_available !== false;
    const canAllowCommand = req?.allow_command_available === true;
    const cwd = req?.cwd ? `<div class="meta"><strong>cwd:</strong> ${esc(req.cwd)}</div>` : '';
    const inputObj = (req?.tool_input && typeof req.tool_input === 'object') ? req.tool_input : {};
    const command = typeof inputObj.command === 'string' ? inputObj.command :
        (typeof inputObj.cmd === 'string' ? inputObj.cmd : '');
    const explanation = typeof inputObj.explanation === 'string' ? inputObj.explanation : '';
    const goal = typeof inputObj.goal === 'string' ? inputObj.goal : '';
    const summary = 'Review this tool request before continuing.';
    const commandBlock = command
        ? `<div class="cmd-label">command</div><pre class="cmd">${esc(command)}</pre>`
        : '';

    const details = `
        ${explanation ? `<div class="meta"><strong>explanation</strong> ${esc(explanation)}</div>` : ''}
        ${goal ? `<div class="meta"><strong>goal</strong> ${esc(goal)}</div>` : ''}
    `;

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
    body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
           background: var(--vscode-editor-background); padding: 16px 18px; }
    h1 { font-size: 15px; font-weight: 600; margin: 0 0 10px; }
    .summary { font-size: 13px; line-height: 1.5; margin-bottom: 14px; }
    .meta { font-size: 12px; color: var(--vscode-descriptionForeground); margin-bottom: 6px; }
        .cmd-label { font-size: 12px; color: var(--vscode-descriptionForeground); margin: 8px 0 4px; }
        pre { white-space: pre-wrap; word-break: break-word; font-family: var(--vscode-editor-font-family, monospace);
          font-size: 12px; padding: 12px; border-radius: 6px; overflow: auto;
          background: var(--vscode-textCodeBlock-background, rgba(128,128,128,0.12));
          border: 1px solid var(--vscode-widget-border, transparent); }
        .cmd { margin: 0 0 10px; }
    .actions { display: flex; gap: 10px; margin-top: 14px; }
    .danger-note { margin-top: 12px; font-size: 12px; line-height: 1.4; color: #ffb74d; }
    .trust-row { margin-top: 8px; }
    .trust-row button { width: 100%; }
    button { font-family: inherit; font-size: 13px; padding: 6px 14px; border: none;
             border-radius: 4px; cursor: pointer; }
    .primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
    .primary:hover { background: var(--vscode-button-hoverBackground); }
    .info { background: #2f6f9f; color: var(--vscode-button-foreground); }
    .info:hover { filter: brightness(1.08); }
    .danger { background: var(--vscode-inputValidation-errorBackground, #5a1d1d);
              color: var(--vscode-button-foreground); }
    .danger:hover { filter: brightness(1.08); }
    .secondary { background: var(--vscode-button-secondaryBackground);
                 color: var(--vscode-button-secondaryForeground); }
    .secondary:hover { background: var(--vscode-button-secondaryHoverBackground); }
</style>
</head>
<body>
    <h1>${esc(name)} requests permission</h1>
    <div class="summary">${summary}</div>
    <div class="meta"><strong>tool</strong> ${toolName}</div>
    ${cwd}
    ${details}
    ${commandBlock}
    <div class="actions">
        <button id="allow" class="primary">Allow</button>
        <button id="deny" class="danger">Deny</button>
        <button id="fallback" class="secondary">Use VS Code prompt</button>
    </div>
    ${canAllowFolder ? `
    <div class="danger-note">Trusting this folder auto-allows read-only inspection and safe, non-delete patches inside it.</div>
    <div class="trust-row">
        <button id="allow-folder" class="info">Allow + Trust Folder for Safe Edits</button>
    </div>
    ` : ''}
    ${canAllowCommand ? `
    <div class="danger-note">Allow this exact command automatically in this repository in future sessions and agents.</div>
    <div class="trust-row">
        <button id="allow-command" class="info">Allow + Always Allow Exact Command Here</button>
    </div>
    ` : ''}
<script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    document.getElementById('allow').addEventListener('click', () => vscode.postMessage({ type: 'allow' }));
    const allowFolder = document.getElementById('allow-folder');
    if (allowFolder) {
        allowFolder.addEventListener('click', () => vscode.postMessage({ type: 'allow_folder' }));
    }
    const allowCommand = document.getElementById('allow-command');
    if (allowCommand) {
        allowCommand.addEventListener('click', () => vscode.postMessage({ type: 'allow_command' }));
    }
    document.getElementById('deny').addEventListener('click', () => vscode.postMessage({ type: 'deny' }));
    document.getElementById('fallback').addEventListener('click', () => vscode.postMessage({ type: 'fallback' }));
</script>
</body>
</html>`;
}

function start(): void {
    client?.stop();
    client = undefined;

    const cfg = vscode.workspace.getConfiguration('codelight');
    if (!cfg.get<boolean>('enabled', true)) {
        statusItem.hide();
        return;
    }

    client = new CodelightClient(
        cfg.get<string>('host', '127.0.0.1'),
        cfg.get<number>('port', 8765),
        cfg.get<string>('secret', ''),
        cfg.get<boolean>('permissionPrompts', true),
        cfg.get<boolean>('questionPrompts', true),
        {
            onStatus: (p) => {
                lastStatus = p;
                if (!pending) { applyStatus(p); }
            },
            onConnectionChange: (up) => {
                if (!up) {
                    disposePanel();
                    pending = undefined;
                    stopKeepalive();
                    statusItem.text = `$(circle-slash) ${agentName(lastStatus)} OFFLINE`;
                    statusItem.tooltip = 'codelight — companion daemon offline';
                    statusItem.command = undefined;
                    statusItem.show();
                }
            },
            onAuthFailed: () => {
                statusItem.text = `$(key) ${agentName(lastStatus)} AUTH FAILED`;
                statusItem.tooltip = 'codelight — wrong or missing secret';
                statusItem.command = undefined;
                statusItem.show();
                vscode.window.showErrorMessage(
                    'codelight: the companion rejected the connection — set codelight.secret ' +
                    "to match the daemon's --secret.",
                    'Open Settings',
                ).then((choice) => {
                    if (choice) {
                        vscode.commands.executeCommand(
                            'workbench.action.openSettings', 'codelight.secret');
                    }
                });
            },
            onPermissionRequest: (req) => { void activateAndShow(req); },
            onQuestionRequest: (req) => { void activateAndShow(req); },
            onRequestResolved: (msg) => {
                const id = String(msg?.id ?? '');
                if (pending && pending.id === id) {
                    disposePanel();
                    clearPending();
                }
            },
        },
    );
    client.start();
}

export function activate(context: vscode.ExtensionContext): void {
    statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    context.subscriptions.push(statusItem);
    context.subscriptions.push(
        vscode.commands.registerCommand('codelight.answerQuestion', () => {
            if (pending) { showRequest(pending); }
        }),
    );
    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('codelight')) { start(); }
        }),
    );
    start();
}

export function deactivate(): void {
    client?.stop();
    client = undefined;
    stopKeepalive();
    disposePanel();
}
