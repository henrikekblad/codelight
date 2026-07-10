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

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 50 50">'
    '<path style="fill:currentColor" d="M19.861,27.625v-0.716l-16.65-0.681L2.07,25.985L1,24.575l0.11-0.703l0.959-0.645l17.95,1.345l0.11-0.314L5.716,14.365l-0.729-0.924l-0.314-2.016L5.985,9.98l2.214,0.24l11.312,8.602l0.327-0.353L12.623,5.977c0,0-0.548-2.175-0.548-2.697l1.494-2.029l0.827-0.266l2.833,0.995l7.935,17.331h0.314l1.348-14.819l0.752-1.822l1.494-0.985l1.167,0.557l0.959,1.374l-2.551,14.294h0.425l0.486-0.486l8.434-10.197l1.092-0.862h2.065l1.52,2.259l-0.681,2.334l-7.996,11.108l0.146,0.217l0.376-0.036l12.479-2.405l1.666,0.778l0.182,0.791l-0.655,1.617l-15.435,3.523l-0.084,0.062l0.097,0.12l13.711,0.814l1.578,1.044L49,29.868l-0.159,0.972l-2.431,1.238l-13.561-3.254h-0.363v0.217l11.218,10.427l0.256,1.154l-0.645,0.911l-0.681-0.097l-9.967-8.058h-0.256v0.34l5.578,8.35l0.243,2.162l-0.34,0.703l-1.215,0.425l-1.335-0.243l-7.863-12.083l-0.279,0.159l-1.348,14.524l-0.632,0.742l-1.459,0.558l-1.215-0.924L21.9,46.597l2.966-14.939l-0.023-0.084l-0.279,0.036L13.881,45.138l-0.827,0.327l-1.433-0.742l0.133-1.326l0.801-1.18l9.52-12.019l-0.013-0.314h-0.11l-12.69,8.239l-2.259,0.292L6.03,37.505l0.12-1.494l0.46-0.486L19.861,27.625z"/>'
    '</svg>'
)

# 48x48 1-bit render of LOGO_SVG for the ESP8266 screen.
LOGO_BITMAP = (
    "AAAAAAAAAAYAAAAAAA+AGAAAAA+APAAAAA/APAAAAA/AOAAAAAfAOAcAAAfgOA8AAAPgOB+"
    "AAAPweB8AB4HweD8AB8D4cH4AB+D4cP4AB/B8cfwAAfx8c/gAAP4+c/gAAD8eZ/AAAB/ef"
    "+AAAAf//8AAAAP//8AMAAD//4H+AAB////8AAA////wf/8f//4AP////+AAAAD//wAAAAA"
    "f///AAAA////8AAD//w/+AAP3/4D+AAfO/8AIAB8c/+AAAH4c3vAAAPg5z3gAA/BxzxwAA"
    "8Dhx44AAQHhg8cAAAHBg8OAAAODgeDAAAcDgPBAAA4DgPAAAA4DgHAAABwDgDAAAAAHgAA"
    "AAAAHgAAAAAADgAAAAAADAAAAAAAAAAAA"
)

SPEC = base.AgentSpec(
    "claude",
    "Claude",
    executables=("claude",),
    vscode_extensions=frozenset({"anthropic.claude-code"}),
    color="#DE7356",
    logo_svg=LOGO_SVG,
    logo_bitmap=LOGO_BITMAP,
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


def build_integration(
    config: dict,
    *,
    usage_api: str = USAGE_API,
    log: Callable[[str], None] | None = None,
) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.claude):
    settings_path, credentials_path."""
    settings_path = (
        os.path.expanduser(str(config.get("settings_path") or ""))
        or default_settings_path())
    credentials_path = (
        os.path.expanduser(str(config.get("credentials_path") or ""))
        or default_credentials_path())
    agent = ClaudeAgent(credentials_path, usage_api=usage_api, log=log)

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
        agent=agent,
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
