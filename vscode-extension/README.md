# codelight — VSCode extension

Shows the active Claude, Copilot, or Codex status in the VS Code status bar.
Hovering shows usage grouped by agent, including optional company Copilot
monthly credits. When detailed usage is unavailable, that agent remains
status-only.

<table>
<tr>
<td><img src="../assets/vscode-status.png" width="269"
         alt="VS Code status bar and grouped codelight usage tooltip"></td>
<td><img src="../assets/vscode-permission.png" width="620"
         alt="VS Code codelight permission review"></td>
</tr>
<tr><td align="center">Status and usage</td><td align="center">Permission review</td></tr>
</table>

## Questions and permissions

When Claude asks a multiple-choice question, a themed **WebView panel** opens
beside your editor with the question(s), options (radio or checkboxes for
multi-select), a free-text "Other…" field, and **Submit** / **Skip** buttons.
The status-bar item turns into an agent question indicator you can click
to reopen the panel if you dismiss it. Whoever answers first (VSCode, the phone,
or GNOME) wins; the panel closes automatically if the question is answered
elsewhere or times out.

Permission requests open a separate review webview with **Allow**, **Deny**, and
fallback actions. A request may also be allowed while trusting the repository
for narrowly safe edits, or while persisting the exact command in that
repository. The policy is shared across Claude, Copilot, and Codex; see
[Persistent folder and command approvals](../companion/README.md#persistent-folder-and-command-approvals).

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
| `codelight.questionPrompts` | `true` | Answer supported agent questions in the editor |
| `codelight.permissionPrompts` | `true` | Review supported agent permission requests in the editor |

The extension also contributes a **codelight: Review pending request** command
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
