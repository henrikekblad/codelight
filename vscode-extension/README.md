# codelight — VSCode extension

Shows the Claude Code status (working / waiting for input / idle, plus usage
%) from the [codelight companion daemon](../companion/) in the VSCode status
bar — and, when the daemon runs with `--remote-control`, **answers Claude's
AskUserQuestion prompts right in the editor**.

## Answering questions

When Claude asks a multiple-choice question, a themed **WebView panel** opens
beside your editor with the question(s), options (radio or checkboxes for
multi-select), a free-text "Other…" field, and **Submit** / **Skip** buttons.
The status-bar item turns into a `$(bell-dot) claude — question` you can click
to reopen the panel if you dismiss it. Whoever answers first (VSCode, the phone,
or GNOME) wins; the panel closes automatically if the question is answered
elsewhere or times out.

Permission prompts (Allow / Deny) are **not** shown in VSCode — inside the
editor you answer Claude Code's own native permission dialog. codelight's remote
*permission* approval is for when you're away from the computer (the Android app
and the GNOME panel). See
[companion/README.md](../companion/README.md#remote-control).

## Install

Easiest: let the companion installer do it — it installs the extension (local
build or latest GitHub release) **and configures `codelight.secret` for you**:

```bash
python3 companion/codelight.py --install --name my-laptop --secret mypassword --vscode
```

`python3 companion/codelight.py --uninstall` removes the extension and its
settings again.

Manual: download `codelight-vX.Y.Z.vsix` from the
[releases page](https://github.com/henrikekblad/codelight/releases) and run
`code --install-extension codelight-vX.Y.Z.vsix`.

## Settings

| Setting | Default | |
|---|---|---|
| `codelight.enabled` | `true` | Connect to the companion daemon |
| `codelight.host` | `127.0.0.1` | Daemon host |
| `codelight.port` | `8765` | Daemon WebSocket port |
| `codelight.secret` | `""` | Must match the daemon's `--secret` |
| `codelight.questionPrompts` | `true` | Answer Claude's AskUserQuestion prompts in the editor |

The extension also contributes a **codelight: Answer Claude's question** command
(bound to the status-bar item) that reopens the question panel while one is
pending.

The extension authenticates with the daemon via an HMAC challenge-response, so
the secret is never sent over the (plaintext `ws://`) connection.

## Development

```bash
npm install
npm run build        # bundle to dist/extension.js
npx vsce package     # produce codelight-*.vsix
```

Press F5 in VSCode with this folder open to launch an Extension Development
Host against your locally running daemon.
