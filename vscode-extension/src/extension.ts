import * as vscode from 'vscode';
import { CodelightClient } from './client';

let client: CodelightClient | undefined;
let statusItem: vscode.StatusBarItem;

const STATUS_ICON: Record<string, string> = {
    working: '$(sync~spin)',
    waiting: '$(bell-dot)',
    idle:    '$(check)',
};

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
        {
            onStatus: (p) => {
                const icon = STATUS_ICON[p.status] ?? '$(circle-outline)';
                statusItem.text = `${icon} claude`;
                statusItem.tooltip =
                    `codelight — ${p.status}\n` +
                    `session ${Math.round((p.session_pct ?? 0) * 100)}% (resets ${p.session_reset})\n` +
                    `weekly ${Math.round((p.weekly_pct ?? 0) * 100)}% (resets ${p.weekly_reset})`;
                statusItem.show();
            },
            onConnectionChange: (up) => {
                if (!up) {
                    statusItem.text = '$(circle-slash) claude';
                    statusItem.tooltip = 'codelight — companion daemon offline';
                    statusItem.show();
                }
            },
            onAuthFailed: () => {
                statusItem.text = '$(key) claude';
                statusItem.tooltip = 'codelight — wrong or missing secret';
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
        },
    );
    client.start();
}

export function activate(context: vscode.ExtensionContext): void {
    statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    context.subscriptions.push(statusItem);
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
}
