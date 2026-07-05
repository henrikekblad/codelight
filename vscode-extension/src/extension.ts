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

function applyStatus(p: any): void {
    const icon = STATUS_ICON[p.status] ?? '$(circle-outline)';
    statusItem.text = `${icon} claude`;
    statusItem.tooltip =
        `codelight — ${p.status}\n` +
        `session ${Math.round((p.session_pct ?? 0) * 100)}% (resets ${p.session_reset})\n` +
        `weekly ${Math.round((p.weekly_pct ?? 0) * 100)}% (resets ${p.weekly_reset})`;
    statusItem.command = undefined;
    statusItem.show();
}

function showPromptStatus(req: any): void {
    const q = req?.questions?.[0]?.question ?? 'Claude has a question';
    statusItem.text = '$(bell-dot) claude — question';
    statusItem.tooltip = `codelight — ${q}\n(click to answer)`;
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
function showQuestion(req: any): void {
    if (panel) { panel.reveal(vscode.ViewColumn.Beside, false); return; }

    panel = vscode.window.createWebviewPanel(
        'codelightQuestion',
        'Claude asks',
        { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
        { enableScripts: true, retainContextWhenHidden: true },
    );
    panel.webview.html = renderHtml(panel.webview, req);

    panel.webview.onDidReceiveMessage((msg) => {
        if (!pending || pending.id !== req.id) { return; }
        if (msg?.type === 'submit') {
            client?.respondQuestion(req.id, msg.answers ?? {});
            clearPending();
            disposePanel();
        } else if (msg?.type === 'skip') {
            client?.respondQuestion(req.id, {});   // empty → Claude's local dialog
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
    showQuestion(req);
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
    const nonce = getNonce();
    const questions = Array.isArray(req.questions) ? req.questions : [];

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
    <h1>Claude asks</h1>
    ${blocks}
    <div id="err" class="err"></div>
    <div class="actions">
        <button id="submit" class="primary">Submit</button>
        <button id="skip" class="secondary">Skip — use Claude's own prompt</button>
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
                    statusItem.text = '$(circle-slash) claude';
                    statusItem.tooltip = 'codelight — companion daemon offline';
                    statusItem.command = undefined;
                    statusItem.show();
                }
            },
            onAuthFailed: () => {
                statusItem.text = '$(key) claude';
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
            onQuestionRequest: (req) => { void activateAndShow(req); },
            onRequestResolved: (id) => {
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
            if (pending) { showQuestion(pending); }
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
