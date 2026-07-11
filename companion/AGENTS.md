# Agent integrations and configuration

codelight keeps agent-specific options in `~/.config/codelight/config.json`.
The main daemon is intentionally agent-agnostic: each built-in agent module
declares an `AgentIntegration` with detection metadata, hook modes, usage
fetching, transcript extraction, removable files, and client branding.

Top-level structure:

```json
{
  "agents": {
    "claude": {},
    "copilot": {},
    "codex": {}
  }
}
```

All keys are optional. If a key is missing, the companion uses the agent's default.

## Integration model

Each file under `companion/codelight_core/agents/` owns the behavior for one
agent and exports `build_integration(config, ...)`. The returned integration
contains:

- `AgentSpec`: id, display name, CLI/VSCode detection metadata, brand color,
  SVG logo, and a 48×48 1-bit bitmap for the ESP8266 screen.
- `hook_modes`: stable `--hook` tokens used by installed hooks for permission
  and question forwarding.
- `usage_fetcher`: optional usage/limit source.
- `install_hooks`: optional hook installer.
- transcript path/extractor callbacks for conversation following.
- removable hook paths/files for `--uninstall`.

The daemon sends agent branding to clients in the WebSocket/D-Bus config
handshake. Clients should render the metadata they receive and avoid hard-coded
agent assets.

To add another agent, add a new agent module and create an `AgentIntegration`.
The registry discovers built-in modules automatically. The public client payload
should not need to change unless the shared schema needs a new capability.

### New integration checklist

1. Add `companion/codelight_core/agents/<agent_id>.py`.
2. Define an `AgentSpec` with:
   - stable `agent_id`
   - display name
   - CLI and/or VSCode detection metadata
   - brand color
   - `currentColor` SVG logo
   - 48×48 1-bit `logo_bitmap` for the screen client
3. Export `build_integration(config, ...)` and return an `AgentIntegration`.
4. Keep all agent-specific behavior in that module:
   - hook installation and uninstall paths
   - hook modes and native prompt envelopes
   - usage fetching
   - transcript path lookup and JSON/event extraction
   - safe read-only tools for trusted-folder auto-allow
5. Run `python3 companion/tools/logo_bitmap.py path/to/logo.svg` if you need to
   generate the screen bitmap from an SVG logo.
6. Add tests with `extra_agents` or a tiny in-memory module where possible so the shared registry/client
   path remains independent of the built-in agents.
7. Document config keys and quirks in this file, not in the general README.
8. Update `companion/config.schema.json` if the integration adds public config
   keys.

The only intentional agent-specific code outside the module should be tests and
documentation.

## Claude (`agents.claude`)

Keys:

- `settings_path` (string): path to Claude settings JSON.
  - Default: `~/.claude/settings.json`
- `credentials_path` (string): path to Claude OAuth credentials file used for usage polling.
  - Default: `~/.claude/.credentials.json`

Behavior and quirks:

- Hooks are merged into the configured Claude settings file.
- Usage is fetched from the Claude OAuth usage API.
- Permission prompts use Claude's `PermissionRequest` decision envelope.
- Question forwarding uses a `PreToolUse` hook matching `AskUserQuestion`.

Example:

```json
{
  "agents": {
    "claude": {
      "settings_path": "~/.claude/settings.json",
      "credentials_path": "~/.claude/.credentials.json"
    }
  }
}
```

## Copilot (`agents.copilot`)

Keys:

- `home` (string): Copilot home directory.
  - Default: `~/.copilot` (or `COPILOT_HOME`)
- `github_org` (string): GitHub organization slug for pooled monthly AI-credit usage.
  - Default: empty (usage disabled)
- `github_token_file` (string): file containing a GitHub token.
  - Default: empty

Token resolution order:

1. `CODELIGHT_GITHUB_TOKEN`
2. `GITHUB_TOKEN`
3. `GH_TOKEN`
4. `agents.copilot.github_token_file`
5. `gh auth token`

Behavior and quirks:

- Copilot hooks are written to a codelight-owned file under `~/.copilot/hooks/codelight.json`.
- Usage is organization-level monthly billing usage, not per-session limits.
- If billing endpoints are unavailable to the token/org, status still works and usage is omitted.
- Permission prompts use Copilot/VSCode-specific hook envelopes.
- Copilot's question support comes through the VSCode-style question hook path.

Example:

```json
{
  "agents": {
    "copilot": {
      "github_org": "Sensnology-AB",
      "github_token_file": "~/.config/codelight/github-token.txt"
    }
  }
}
```

## Codex (`agents.codex`)

Keys:

- `home` (string): Codex home directory.
  - Default: `~/.codex` (or `CODEX_HOME`)
- `app_server_usage` (boolean): use `codex app-server` for
  `account/rateLimits/read` usage and earned reset-credit metadata when
  available.
  - Default: `true`

Behavior and quirks:

- Hooks are merged into `~/.codex/hooks.json`.
- Codex requires hook trust review in the Codex CLI (`/hooks`) when hooks change.
- Usage is read from local rollout JSONL rate-limit events (5-hour and weekly windows).
- When `codex app-server` is available, usage prefers
  `account/rateLimits/read` so codelight can also show
  `rateLimitResetCredits.availableCount`.
- Session reset consumes one earned reset via
  `account/rateLimitResetCredit/consume`, then refreshes with
  `account/rateLimits/read`.
- Local Codex CLI and Codex IDE extension sessions share the user-level hooks file.
- The question tool is `request_user_input`. It is available in Plan Mode by
  default, or in Default Mode when enabled with:

  ```toml
  [features]
  default_mode_request_user_input = true
  ```

- Codex lifecycle hooks cannot submit a native `request_user_input` response.
  Codelight therefore uses an experimental fallback: after a remote answer it
  blocks the local question tool and injects the answer into model context. If
  no question-capable client is connected, or the request times out, codelight
  emits no hook decision and Codex shows its normal local question UI.
- Codex requires non-managed command hooks to be reviewed by hash. After the
  initial install — or whenever Codex says a new/changed hook needs review —
  open Codex CLI and run `/hooks`, inspect the codelight commands, and trust
  them. This cannot safely be persisted by the Python installer. Codex offers
  `--dangerously-bypass-hook-trust` for a single already-vetted automation
  launch, but codelight deliberately does not make it a permanent bypass.

Example:

```json
{
  "agents": {
    "codex": {
      "home": "~/.codex",
      "app_server_usage": true
    }
  }
}
```

## Grok (`agents.grok`)

Keys:

- `home` (string): Grok home directory.
  - Default: `~/.grok` (or `GROK_HOME`)

Behavior and quirks:

- Status-only integration: codelight writes its own hook file
  `~/.grok/hooks/codelight.json` (Grok reads every `*.json` in that
  directory) mapping SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/
  PostToolUseFailure/PermissionDenied/Subagent* → working,
  Notification → waiting, Stop/SessionEnd → ended.
- **No remote permission approval**: Grok's only blocking hook
  (`PreToolUse`) can deny but cannot approve past Grok's own interactive
  prompt, and it fires for every tool call before Grok's permission
  pipeline — forwarding it would spam clients with requests Grok
  auto-approves. Revisit if xAI adds an allow/bypass decision.
- No usage meters yet: no machine-readable quota surface has been found for
  the CLI. Clients hide the bars (empty meter titles).
- Session transcripts: layout under `~/.grok/sessions` is undocumented;
  codelight looks up session files by id in the filename, best-effort.
- Auth for the CLI itself: browser login (SuperGrok / X Premium+) or
  `XAI_API_KEY` — irrelevant to codelight's hooks either way.

Example:

```json
{
  "agents": {
    "grok": {
      "home": "~/.grok"
    }
  }
}
```

## Cursor (`agents.cursor`)

Keys:

- `home` (string): Cursor home directory.
  - Default: `~/.cursor` (or `CURSOR_HOME`)
- `state_db` (string): Cursor IDE's SQLite state store, used to read the
  auth token for the usage meter.
  - Default: `~/.config/Cursor/User/globalStorage/state.vscdb` (Linux; set
    this on macOS/Windows).
- `usage` (boolean): show the monthly usage meter. Default: `true`.

Behavior and quirks:

- Usage meter: reads Cursor's session JWT from `state_db` (no cookie paste),
  then calls Cursor's own `cursor.com/api/usage-summary` and shows
  `totalPercentUsed` as a monthly bar resetting at the billing-cycle end.
  Undocumented endpoint — if it changes or you're not signed in, the meter
  simply hides (returns nothing). Set `usage: false` to disable the call.

- Hooks are **merged into the user's own `~/.cursor/hooks.json`** (flat
  entry format, `{"version": 1, "hooks": {...}}`): codelight entries are
  identified by command and stripped/replaced on reinstall/uninstall; the
  user's own hooks are never touched.
- Status: sessionStart/beforeSubmitPrompt/postToolUse(+Failure) → working,
  stop/sessionEnd → ended. Cursor's session key is `conversation_id` and
  every hook payload carries `transcript_path`, which feeds the
  conversation feature automatically.
- **Full remote permission support** via `beforeShellExecution` and
  `beforeMCPExecution` (`permission-cursor` hook mode): allow bypasses
  Cursor's own prompt, deny blocks, and when no remote decision arrives
  codelight answers `{"permission": "ask"}` so Cursor falls back to its
  local prompt. `preToolUse` is deliberately not used for permissions
  (allow/deny only — no safe fallback).
- No question interception (Cursor has no AskUserQuestion-style hook).
- No usage meters: Cursor has no official individual usage API (the
  dashboard API requires a browser session cookie — see PLAN.md).
- **Remote approval works in the Cursor IDE** (verified 2026-07-11): a
  `beforeShellExecution` `allow` bypasses the IDE's command prompt, so a
  remote Allow runs the command with no local prompt.
- **The Cursor CLI (`cursor-agent`) is weaker — deny-only in practice.** Its
  hook set is a subset, and a hook `allow` does NOT bypass the CLI's own
  command allowlist ("Not in allowlist: …"), so you get a double prompt
  (phone + TUI) and the command still waits for local approval. In headless
  `-p` there's no prompt at all: commands need `--force`/`--yolo`, and a hook
  `allow` won't auto-run them (Cursor retries to its loop limit, then gives
  up). Remote *deny* does work everywhere (hooks can always block). Net: use
  the IDE for the full remote-approval experience; on the CLI, codelight is
  effectively status + remote-deny.
- Detection: `cursor` (IDE) or `cursor-agent` (CLI) on PATH. The generic
  `agent` alias is deliberately not probed.

Example:

```json
{
  "agents": {
    "cursor": {
      "home": "~/.cursor"
    }
  }
}
```

## Combined example

```json
{
  "agents": {
    "claude": {
      "settings_path": "~/.claude/settings.json",
      "credentials_path": "~/.claude/.credentials.json"
    },
    "copilot": {
      "github_org": "Sensnology-AB",
      "github_token_file": "~/.config/codelight/github-token.txt"
    },
    "codex": {
      "home": "~/.codex"
    }
  }
}
```

## After changing config

Restart the companion service:

```bash
systemctl --user restart codelight.service
systemctl --user is-active codelight.service
```

## Remote control quirks

The shared remote-control path normalizes permission and question prompts, but
the native hook envelopes differ:

| Agent | Permission hook | Question hook | Notes |
|---|---|---|---|
| Claude | `PermissionRequest` → permission decision | `PreToolUse`/`AskUserQuestion` → updated input | Falls back to Claude's native dialog when no remote answer is available. |
| Copilot | Copilot/VSCode behavior envelope | VSCode-style context envelope | Primarily supports permission review; questions follow the VSCode hook path where available. |
| Codex | `PermissionRequest` → permission decision | `PreToolUse`/`request_user_input` → context injection | Remote question answering is experimental because lifecycle hooks cannot submit native tool responses. |

Persistent folder and exact-command approvals are not agent-specific. They are
stored in `~/.config/codelight/policy.json` and enforced by the shared hook
runtime before a request is sent to clients.
