# Agent integrations and configuration

codelight keeps agent-specific options in `~/.config/codelight/config.json`.

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

## Claude (`agents.claude`)

Keys:

- `settings_path` (string): path to Claude settings JSON.
  - Default: `~/.claude/settings.json`
- `credentials_path` (string): path to Claude OAuth credentials file used for usage polling.
  - Default: `~/.claude/.credentials.json`

Behavior and quirks:

- Hooks are merged into the configured Claude settings file.
- Usage is fetched from the Claude OAuth usage API.
- Claude and Codex share the same permission/question hook envelope format for remote control.

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

Behavior and quirks:

- Hooks are merged into `~/.codex/hooks.json`.
- Codex requires hook trust review in the Codex CLI (`/hooks`) when hooks change.
- Usage is read from local rollout JSONL rate-limit events (5-hour and weekly windows).

Example:

```json
{
  "agents": {
    "codex": {
      "home": "~/.codex"
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
