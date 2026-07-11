from __future__ import annotations

import json
import os

from codelight_core import hooks as hooks_core
from codelight_core.agents import base


# Grok's swirl mark (lobe-icons), fills with currentColor.
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" fill-rule="evenodd" d="M9.27 15.29l7.978-5.897'
    'c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.'
    '667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2'
    '.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.'
    '383.06-.082.12-.164.179-.248l-3.301 3.305v-.01L9.267 15.292M7.623 16.723'
    'c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.'
    '705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.53'
    '6-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199'
    ' 1.259-1.682 1.925l7.62-6.815"/></svg>'
)

# 48x48 1-bit render of LOGO_SVG for the ESP8266 screen.
LOGO_BITMAP = (
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAIAAAB4AAQAAA//gBwAAD//4Dg"
    "AAH//+HgAAf//4HAAA/8HgPAAB/gAAfAAD+AAA/AAD8AAB+AAH4AAD/AAHwAAH/AAPwAAP/A"
    "APgAAe/AAfAAA8fAAfAABwfAAfAADgfAAfAAGAPAAfAAMAPgAeAAYAPgAfAAgAPgAfABAAPA"
    "AfACAAfAAfAAAAfAAfgAAAfAAPgAAA/AAPwAAA+AAPwAAB+AAH4AAD8AAHwAAH4AAPgAAP4A"
    "APAAA/wAAOB///gAAcH///AAAQH//8AAAgB//4AABAAP/AAACAAAAAAAEAAAAAAAIAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAA"
)

SPEC = base.AgentSpec(
    "grok",
    "Grok",
    executables=("grok",),
    color="#FFFFFF",
    logo_svg=LOGO_SVG,
    logo_bitmap=LOGO_BITMAP,
)

# No permission/question hook modes: Grok's PreToolUse hook can only DENY —
# it cannot approve past Grok's own interactive prompt — and it fires for
# every tool call (before Grok's permission pipeline), so forwarding it as a
# remote prompt would spam clients with requests Grok auto-approves anyway.
# Status hooks cover the codelight experience; revisit if xAI adds an
# allow/bypass decision (see PLAN.md watch item).
HOOK_MODES: tuple[base.HookMode, ...] = ()


def default_home() -> str:
    return os.path.expanduser(os.environ.get("GROK_HOME", "~/.grok"))


def hooks_path(grok_home: str) -> str:
    # Grok reads every *.json under its hooks dir; codelight owns this one.
    return os.path.join(grok_home, "hooks", "codelight.json")


def sessions_path_for_session(grok_home: str, session_id: str) -> str:
    """Best-effort lookup of Grok's session file for a hook session id.

    The on-disk layout under ~/.grok/sessions is undocumented; match any
    file carrying the session id in its name, newest first.
    """
    sid = str(session_id or "").strip()
    if not sid or sid == "unknown":
        return ""
    try:
        bases = os.path.realpath(os.path.join(grok_home, "sessions"))
        newest_path = ""
        newest_mtime = 0.0
        for root, _, files in os.walk(bases):
            for name in files:
                if sid not in name:
                    continue
                path = os.path.realpath(os.path.join(root, name))
                if not path.startswith(bases + os.sep):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest_path = path
        return newest_path
    except Exception:
        return ""


class GrokAgent:
    def __init__(self, grok_home: str) -> None:
        self.grok_home = grok_home

    def session_path_for_session(self, session_id: str) -> str:
        return sessions_path_for_session(self.grok_home, session_id)


def install_hooks(
    hooks_file: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    permission_timeout: int = 60,
) -> None:
    """Write codelight's status hooks (full-document, codelight owns the file).

    remote_permissions is accepted for interface parity but ignored — Grok's
    hooks cannot approve tool use remotely (deny-only PreToolUse).
    """
    cmd_base = hooks_core.hook_command_base(script_path, "grok")

    def command(state: str) -> list[dict]:
        return [{"type": "command", "command": f"{cmd_base} {state}"}]

    doc = {
        "hooks": {
            "SessionStart":       command("working"),
            "UserPromptSubmit":   command("working"),
            "PreToolUse":         command("working"),
            "PostToolUse":        command("working"),
            "PostToolUseFailure": command("working"),
            "PermissionDenied":   command("working"),
            "SubagentStart":      command("working"),
            "SubagentStop":       command("working"),
            "Notification":       command("waiting"),
            "Stop":               command("ended"),
            "SessionEnd":         command("ended"),
        },
    }

    try:
        with open(hooks_file) as stream:
            existing = json.load(stream)
    except Exception:
        existing = {}
    if existing == doc:
        print(f"[grok-hooks] already up to date in {hooks_file}", flush=True)
        return
    hooks_core.write_json_object(hooks_file, doc)
    print(f"[grok-hooks] installed in {hooks_file}", flush=True)


def build_integration(config: dict) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.grok): home."""
    home = (os.path.expanduser(str(config.get("home") or ""))
            or default_home())
    agent = GrokAgent(home)
    hooks_file = hooks_path(home)

    def _install_hooks(*, script_path, hook_wait_ceiling, remote_permissions,
                       remote_questions, permission_timeout, log=None):
        install_hooks(
            hooks_file,
            script_path,
            hook_wait_ceiling=hook_wait_ceiling,
            remote_permissions=remote_permissions,
            permission_timeout=permission_timeout,
        )

    return base.AgentIntegration(
        spec=SPEC,
        agent=agent,
        hook_modes=HOOK_MODES,
        usage_fetcher=None,   # no machine-readable quota surface found yet
        install_hooks=_install_hooks,
        removable_files=(hooks_file,),
        removable_empty_dirs=(os.path.dirname(hooks_file),),
        transcript_path_for_session=agent.session_path_for_session,
    )
