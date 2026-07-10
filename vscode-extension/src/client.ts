import WebSocket from 'ws';
import { createHmac } from 'crypto';

export interface CodelightHandlers {
    onConfig(config: any): void;
    onStatus(payload: any): void;
    onConnectionChange(connected: boolean): void;
    onAuthFailed(): void;
    onPermissionRequest(req: any): void;
    onQuestionRequest(req: any): void;
    onRequestResolved(msg: any): void;
}

/** WebSocket client for the companion daemon: HMAC auth → status stream, with
 *  exponential-backoff reconnect. Also answers AskUserQuestion prompts when
 *  question-answering is enabled (permission approval is left to Claude Code's
 *  own native dialog / the phone / GNOME). */
export class CodelightClient {
    private ws?: WebSocket;
    private stopped = false;
    private backoff = 1000;
    private timer?: NodeJS.Timeout;

    constructor(
        private host: string,
        private port: number,
        private secret: string,
        private answerPermissions: boolean,
        private answerQuestions: boolean,
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

    /** Send a JSON message if the socket is open. */
    send(obj: unknown): void {
        try { this.ws?.send(JSON.stringify(obj)); } catch { /* ignore */ }
    }

    respondQuestion(id: string, answers: Record<string, string>): void {
        this.send({ type: 'question_response', id, answers });
    }

    respondPermission(
        id: string,
        decision: 'allow' | 'allow_folder' | 'allow_command' | 'deny' | 'skip',
    ): void {
        this.send({ type: 'permission_response', id, decision });
    }

    extend(id: string): void {
        this.send({ type: 'extend', id });
    }

    private connect(): void {
        if (this.stopped) { return; }
        const ws = new WebSocket(`ws://${this.host}:${this.port}`);
        this.ws = ws;

        const hello = () => {
            const features = [] as string[];
            if (this.answerPermissions) { features.push('permissions'); }
            if (this.answerQuestions) { features.push('questions'); }
            ws.send(JSON.stringify({ type: 'subscribe', features, client: 'vscode' }));
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
            } else if (m.type === 'config') {
                this.handlers.onConfig(m);
            } else if (m.type === 'permission_request') {
                this.handlers.onPermissionRequest(m);
            } else if (m.type === 'question_request') {
                this.handlers.onQuestionRequest(m);
            } else if (m.type === 'question_resolved' || m.type === 'permission_resolved') {
                this.handlers.onRequestResolved(m);
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
