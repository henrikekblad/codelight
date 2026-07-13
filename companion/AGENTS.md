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

- Hooks are merged into `~/.codex/hooks.json`; the local Codex CLI and the
  Codex IDE extension share this user-level file.
- **You must trust the hooks in Codex.** After the first install — or whenever
  Codex says a new/changed hook needs review — open the Codex CLI, run
  `/hooks`, inspect the codelight commands, and trust them. Codex verifies
  hooks by hash so the installer can't do this for you, and codelight
  deliberately avoids the permanent `--dangerously-bypass-hook-trust`.
- Usage shows Codex's own rolling limits (5-hour and weekly windows), plus
  earned reset credits when `codex app-server` is available.
- The question tool is `request_user_input`, available in Plan Mode by default
  or in Default Mode when enabled:

  ```toml
  [features]
  default_mode_request_user_input = true
  ```

- Remote question answering for Codex is **experimental** (Codex's hooks can't
  submit a native question response — see `PLAN.md` for the workaround).

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
- `management_key` / `management_key_file` (string): xAI billing management key
  for an optional usage meter. **Usually leave this off** — see "No usage
  meter" below. (Or set `XAI_MANAGEMENT_KEY`.)

Requirements:

- The Grok CLI needs a **SuperGrok / X Premium+ subscription**; pay-as-you-go
  API credits (`XAI_API_KEY`) do not run it. Status and conversation therefore
  only apply to subscribers who actually run `grok`.
- On install, codelight writes its hooks to `~/.grok/hooks/codelight.json` and
  turns off Grok's Claude/Cursor "harness compatibility" hooks in
  `~/.grok/config.toml` so Grok reports through its own hooks. Restart any
  running `grok` session after the first install to pick this up.

What works: status (working / waiting / idle) and conversation following.

Limitations:

- **No remote permission approval.** Grok's only blocking hook can deny but
  not approve past its own prompt, so codelight is status + conversation only.
- **No usage meter.** The subscription's weekly limit is not machine-readable,
  and the optional management-key meter reads a *different* xAI wallet (the
  developer-API credits) that the subscription CLI never spends — so it would
  always read ~0%. Leave the management key unset unless you actually use the
  developer API directly. (Rationale and internals are in the maintenance
  notes: `PLAN.md`.)

Example:

```json
{
  "agents": {
    "grok": {
      "home": "~/.grok",
      "management_key_file": "~/.config/codelight/xai-management-key"
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

- **Usage meter:** shows Cursor's monthly included-usage as a bar, read from
  your local Cursor sign-in (no setup). Hides if you're not signed in to the
  IDE, or if Cursor's (undocumented) usage endpoint changes. Set
  `usage: false` to disable it.
- Hooks are **merged into your own `~/.cursor/hooks.json`** — codelight's
  entries are added and removed on install/uninstall; your own hooks are
  never touched.
- Conversation follows automatically (every hook payload carries the
  transcript path).
- **Full remote permission approval in the Cursor IDE** (verified
  2026-07-11): a remote Allow bypasses the IDE's command prompt, Deny blocks,
  and with no remote answer Cursor falls back to its own prompt.
- **The Cursor CLI (`cursor-agent`) is weaker — effectively status +
  remote-deny.** A hook `allow` does not bypass the CLI's own command
  allowlist, so you get a double prompt (phone + TUI) and the command still
  waits for local approval; headless `-p` needs `--force`. Remote *deny* works
  everywhere. Use the IDE for the full remote-approval experience.
- No remote question answering: Cursor has no agent-asks-the-user hook.
- Detection: `cursor` (IDE) or `cursor-agent` (CLI) on PATH.

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

## OpenCode (`agents.opencode`)

Keys:

- `server_url` (string): OpenCode server base URL. Default `http://127.0.0.1:4096`.
- `username` / `password` (string): basic auth, if the server requires it
  (or set `OPENCODE_SERVER_PASSWORD`). Default user `opencode`.
- `db_path` (string): SQLite store. Default `~/.local/share/opencode/opencode.db`.
- `monthly_budget_usd` (number): opt-in cost meter (see below).

Requirements:

- OpenCode has **no hooks**; codelight follows its local HTTP server's event
  stream instead. Run the server on a known port — `opencode serve --port 4096`
  (or the TUI with `--port 4096`) — or point `server_url` at it. A TUI started
  without `--port` picks a random port codelight can't discover.

What works: status (working/waiting/idle), remote permission approval **for
API-initiated prompts** (see the TUI caveat below), **remote question
answering**, **conversation following**, and **remote steering** — OpenCode is
the one agent codelight can actively drive, not just observe.

Behavior and quirks:

- Status: working/idle come from polling the authoritative active-session set
  (`GET /api/session/active`) — OpenCode (v1.17) emits no idle event, only
  activity events + heartbeats. The SSE bus (`GET /event`) supplies the waiting
  edge, routed to remote control; the reply is POSTed back.
- **Permission caveat — TUI vs API (v1 vs v2).** The permission form depends on
  what initiated the turn. A prompt sent through the *server API* (including
  codelight's own remote steering) raises `permission.v2.asked`, answered at
  `/api/session/{id}/permission/{id}/reply` `{reply: once|always|reject}` — this
  round-trips correctly (verified: reply → 204 → the tool runs). A prompt typed
  in the **interactive TUI** instead raises the legacy `permission.asked`
  (v1, `permission`/`patterns` fields); codelight replies at
  `/api/session/{id}/permissions/{id}` `{response: …}` and the server accepts it
  (200), **but the TUI owns that prompt locally and does not dismiss/proceed on
  an external reply** (the permission isn't even in the server's
  `/api/permission/request` list). So remote approval works for prompts you
  *drive from codelight* (steering) or the API, not for prompts typed at the
  desktop TUI — analogous to the Cursor-CLI caveat. codelight handles both v1
  and v2 shapes; the TUI limitation is on OpenCode's side.
- Question answering + conversation use the same SSE/HTTP path
  (`/question/{id}/reply`, `/api/session/{id}/message`).
- **Usage — no provider quota (BYOK):** the only metric is cost. Opt in with
  `monthly_budget_usd` to show this calendar month's spend (summed from the
  store's per-session `cost`, no pricing table) vs that budget. Hidden when
  unset. This is a self-set **tracking** budget — codelight cannot cap
  OpenCode spend (the real bill is at your provider).
- Conversation: read on demand from the server API (active session's
  `GET /api/session/{id}/message`, else the most-recently-updated session) —
  not a JSONL file, so it needs the server running (like status). System
  messages are dropped; assistant tool calls show as a `⚙ <tool>` line.
- Detection: `opencode` on PATH.

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

## Persistent approvals

Folder and exact-command approvals are not agent-specific: they are stored
once in `~/.config/codelight/policy.json` and enforced for every agent before
a request is sent to clients. Each agent section above notes how its own
permission and question prompts behave.
