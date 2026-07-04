# codelight — VSCode extension

Shows the Claude Code status (working / waiting for input / idle, plus usage
%) from the [codelight companion daemon](../companion/) in the VSCode status
bar.

Permission approval is intentionally **not** shown in VSCode — inside the
editor you answer Claude Code's own native permission dialog. codelight's
remote approval (`--remote-permissions`) is for when you're away from the
computer: the Android app and the GNOME notification. See
[companion/README.md](../companion/README.md#remote-permission-approval).

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
