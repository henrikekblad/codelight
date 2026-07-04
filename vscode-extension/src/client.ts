import WebSocket from 'ws';
import { createHmac } from 'crypto';

export interface CodelightHandlers {
    onStatus(payload: any): void;
    onConnectionChange(connected: boolean): void;
    onAuthFailed(): void;
}

/** WebSocket client for the companion daemon: HMAC auth → status stream, with
 *  exponential-backoff reconnect. Status only — permission approval in VSCode
 *  is left to Claude Code's own native dialog. */
export class CodelightClient {
    private ws?: WebSocket;
    private stopped = false;
    private backoff = 1000;
    private timer?: NodeJS.Timeout;

    constructor(
        private host: string,
        private port: number,
        private secret: string,
        private handlers: CodelightHandlers,
    ) {}

    start(): void {
        this.stopped = false;
        this.connect();
    }

    stop(): void {
        this.stopped = true;
        if (this.timer) { clearTimeout(this.timer); }
        try { this.ws?.close(); } catch { /* ignore */ }
    }

    private connect(): void {
        if (this.stopped) { return; }
        const ws = new WebSocket(`ws://${this.host}:${this.port}`);
        this.ws = ws;

        const hello = () => {
            ws.send(JSON.stringify({ type: 'subscribe', features: [], client: 'vscode' }));
        };

        ws.on('open', () => {
            // With a secret the daemon sends a challenge first (see below);
            // without one there is no handshake — say hello right away.
            if (!this.secret) { hello(); }
            this.handlers.onConnectionChange(true);
        });

        ws.on('message', (data) => {
            let m: any;
            try { m = JSON.parse(data.toString()); } catch { return; }
            if (m.error === 'unauthorized') {
                this.stopped = true;   // wrong secret — stop until config changes
                this.handlers.onAuthFailed();
                return;
            }
            this.backoff = 1000;
            if (m.type === 'challenge') {
                const proof = createHmac('sha256', this.secret)
                    .update(String(m.nonce)).digest('hex');
                ws.send(JSON.stringify({ auth_hmac: proof }));
                hello();
            } else if (typeof m.status === 'string') {
                this.handlers.onStatus(m);
            }
        });

        const retry = () => {
            if (this.stopped) { return; }
            this.handlers.onConnectionChange(false);
            if (this.timer) { clearTimeout(this.timer); }
            this.timer = setTimeout(() => this.connect(), this.backoff);
            this.backoff = Math.min(this.backoff * 2, 30_000);
        };
        ws.on('close', retry);
        ws.on('error', () => { /* 'close' follows and triggers the retry */ });
    }
}
