from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

from codelight_core import hooks as hooks_core
from codelight_core.agents import base
from codelight_core.timefmt import epoch, format_iso_countdown


USAGE_API = "https://claude.ai/api/oauth/usage"

SPEC = base.AgentSpec(
    "claude",
    "Claude",
    executables=("claude",),
    vscode_extensions=frozenset({"anthropic.claude-code"}),
)

# Codex reuses the Claude hook protocol for permissions/questions via --agent codex.
HOOK_MODES = (
    base.HookMode("permission", kind="permission",
                  envelope=base.PERMISSION_REQUEST, default_agent_id="claude"),
    base.HookMode("question", kind="question",
                  envelope=base.UPDATED_INPUT, default_agent_id="claude"),
)


def transcript_extractor(record: dict, tool_summary) -> tuple[str, object] | None:
    """Match Claude Code's transcript JSONL: {"type": "user"|"assistant", "message": ...}."""
    t = str(record.get("type") or "").strip().lower()
    if t not in ("user", "assistant"):
        return None
    msg = record.get("message")
    if isinstance(msg, dict):
        role = str(msg.get("role") or t)
        content = msg.get("content")
        if content is not None:
            return role, content
    if isinstance(msg, str):
        return t, msg
    return None


def build_integration(agent: ClaudeAgent, *, settings_path: str) -> base.AgentIntegration:
    def _install_hooks(*, script_path, hook_wait_ceiling, remote_permissions,
                       remote_questions, permission_timeout, log=None):
        install_hooks(
            settings_path,
            script_path,
            hook_wait_ceiling=hook_wait_ceiling,
            remote_permissions=remote_permissions,
            remote_questions=remote_questions,
            permission_timeout=permission_timeout,
            vprint=log,
        )

    return base.AgentIntegration(
        spec=SPEC,
        hook_modes=HOOK_MODES,
        usage_fetcher=agent.get_usage,
        install_hooks=_install_hooks,
        removable_hook_paths=(settings_path,),
        transcript_extractor=transcript_extractor,
    )


def default_settings_path() -> str:
    return os.path.expanduser("~/.claude/settings.json")


def default_credentials_path() -> str:
    return os.path.expanduser("~/.claude/.credentials.json")


class ClaudeAgent:
    def __init__(
        self,
        credentials_path: str,
        *,
        usage_api: str = USAGE_API,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.usage_api = usage_api
        self.log = log

    def get_usage(self) -> dict | None:
        return get_usage(
            self.credentials_path,
            usage_api=self.usage_api,
            log=self.log,
        )


def get_usage(
    credentials_path: str,
    *,
    usage_api: str = USAGE_API,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Fetch Claude OAuth usage.

    Returns session/weekly usage percentages and reset metadata, or None when
    credentials/API access are unavailable.
    """
    try:
        with open(credentials_path) as f:
            creds = json.load(f)
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception as e:
        print(f"[usage] could not read credentials: {e}", file=sys.stderr, flush=True)
        return None

    req = urllib.request.Request(
        usage_api,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/1.0.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"[usage] HTTP {e.code}: {e.reason}", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"[usage] request error: {e}", file=sys.stderr, flush=True)
        return None

    session = data.get("five_hour") or {}
    weekly = data.get("seven_day") or {}

    session_pct = float(session.get("utilization") or 0.0) / 100.0
    weekly_pct = float(weekly.get("utilization") or 0.0) / 100.0
    if log:
        log(f"[usage] API: session={session_pct:.0%} weekly={weekly_pct:.0%}")
    return {
        "session_pct": session_pct,
        "weekly_pct": weekly_pct,
        "session_reset": format_iso_countdown(session.get("resets_at", "")),
        "weekly_reset": format_iso_countdown(weekly.get("resets_at", "")),
        "session_reset_at": epoch(session.get("resets_at", "")),
        "weekly_reset_at": epoch(weekly.get("resets_at", "")),
    }


def install_hooks(
    settings_path: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    remote_questions: bool = False,
    permission_timeout: int = 60,
    vprint: Callable[[str], None] | None = None,
) -> None:
    cmd_base = hooks_core.hook_command_base(script_path, "claude")
    if remote_permissions:
        perm_hook = hooks_core.command_hook(
            f"{cmd_base} permission --permission-timeout {permission_timeout}",
            timeout=hook_wait_ceiling + 15)
    else:
        perm_hook = hooks_core.command_hook(f"{cmd_base} waiting")

    desired: list[hooks_core.HookSpec] = [
        ("PreToolUse",        "", hooks_core.command_hook(f"{cmd_base} working")),
        ("PostToolUse",       "", hooks_core.command_hook(f"{cmd_base} working")),
        ("UserPromptSubmit",  "", hooks_core.command_hook(f"{cmd_base} working")),
        ("PermissionRequest", "", perm_hook),
        ("PermissionDenied",  "", hooks_core.command_hook(f"{cmd_base} working")),
        ("Stop",              "", hooks_core.command_hook(f"{cmd_base} ended")),
        ("SessionEnd",        "", hooks_core.command_hook(f"{cmd_base} ended")),
    ]
    if remote_questions:
        desired.append(("PreToolUse", "AskUserQuestion", {
            "type": "command",
            "command": f"{cmd_base} question --permission-timeout {permission_timeout}",
            "timeout": hook_wait_ceiling + 15}))

    hooks_core.install_matcher_group_hooks(
        settings_path, desired, "hooks", vprint=vprint)
