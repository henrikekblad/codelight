from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from codelight_core import hooks as hooks_core
from codelight_core.agents import base
from codelight_core.timefmt import format_epoch_countdown


MANAGEMENT_API = "https://management-api.x.ai"


def _management_key(config: dict) -> str:
    """Billing management key (separate from the inference key): env, file, or
    inline config. Never logged."""
    env = os.environ.get("XAI_MANAGEMENT_KEY", "").strip()
    if env:
        return env
    key_file = str(config.get("management_key_file") or "").strip()
    if key_file:
        try:
            with open(os.path.expanduser(key_file)) as stream:
                return stream.read().strip()
        except Exception:
            return ""
    return str(config.get("management_key") or "").strip()


def _next_month_start(now: datetime) -> int:
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


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


_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)


def sessions_path_for_session(grok_home: str, session_id: str) -> str:
    """Grok's chat history for a session id.

    Layout is ~/.grok/sessions/<url-encoded-cwd>/<session-id>/chat_history.jsonl
    — the id is a directory name, not part of a file name.
    """
    sid = str(session_id or "").strip()
    if not sid or sid == "unknown":
        return ""
    try:
        base = os.path.realpath(os.path.join(grok_home, "sessions"))
        for root, dirs, _ in os.walk(base):
            if os.path.basename(root) == sid:
                path = os.path.join(root, "chat_history.jsonl")
                if os.path.isfile(path):
                    return path
    except Exception:
        pass
    return ""


def latest_transcript_path(grok_home: str) -> str:
    """Newest chat_history.jsonl across all Grok sessions (cold-start)."""
    try:
        base = os.path.realpath(os.path.join(grok_home, "sessions"))
        newest_path = ""
        newest_mtime = 0.0
        for root, _, files in os.walk(base):
            if "chat_history.jsonl" not in files:
                continue
            path = os.path.join(root, "chat_history.jsonl")
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


def transcript_extractor(record: dict, tool_summary) -> tuple[str, object] | None:
    """Grok's chat_history.jsonl records.

    user: content is a list of text blocks, often synthetic context injections
    (<user_info>, <system-reminder>) — only the real <user_query> is kept.
    assistant: content is a plain string, optionally with tool_calls.
    tool_result: plain string output. reasoning/system are dropped.
    """
    t = str(record.get("type") or "").strip().lower()

    if t == "user":
        if record.get("synthetic_reason"):
            return None
        content = record.get("content")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content or []
            if isinstance(b, dict) and b.get("type") == "text")
        match = _USER_QUERY_RE.search(text)
        if match:
            return "user", match.group(1).strip()
        if text.lstrip().startswith("<"):
            return None   # bare system/context block, not a real prompt
        return ("user", text.strip()) if text.strip() else None

    if t == "assistant":
        blocks: list = []
        content = record.get("content")
        if isinstance(content, str) and content.strip():
            blocks.append({"type": "text", "text": content})
        for call in record.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            args = call.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"input": args}
            if not isinstance(args, dict):
                args = {"input": args}
            blocks.append({"type": "tool_use",
                           "name": str(call.get("name") or "tool"),
                           "input": args})
        return ("assistant", blocks) if blocks else None

    if t == "tool_result":
        text = str(record.get("content") or "").strip()
        return ("output", "⤷ " + text[:400]) if text else None

    return None


def _management_get(key: str, path: str) -> dict | None:
    req = urllib.request.Request(
        MANAGEMENT_API + path, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def get_usage(
    management_key: str,
    *,
    team_id: str = "",
    log: Callable[[str], None] | None = None,
) -> tuple[dict | None, str]:
    """Grok's monthly postpaid budget usage via the xAI Management API.

    Returns (usage, team_id): the percentage of the team's effective spending
    limit consumed this billing cycle, plus the resolved team id (cached by the
    caller). Usage is None when unconfigured, when no spending limit is set
    (nothing to meter), or on any error — never raises, never logs the key.
    """
    if not management_key:
        return None, team_id
    try:
        if not team_id:
            teams = (_management_get(management_key, "/auth/teams") or {}).get("teams") or []
            if not teams:
                return None, ""
            # Prefer a team that isn't blocked (a $0/no-billing starter team is
            # blocked with SPENDING_LIMIT and holds no credits); else the first.
            usable = next((t for t in teams if not t.get("blockedReasons")), teams[0])
            team_id = str(usable.get("teamId") or "")
            if not team_id:
                return None, ""
        preview = _management_get(
            management_key,
            f"/v1/billing/teams/{team_id}/postpaid/invoice/preview")
    except urllib.error.HTTPError as e:
        if log:
            log(f"[grok-usage] HTTP {e.code}: {e.reason}")
        return None, team_id
    except Exception as e:
        if log:
            log(f"[grok-usage] request failed: {e}")
        return None, team_id

    if not isinstance(preview, dict):
        return None, team_id

    def cents(value) -> float:
        if isinstance(value, dict):
            value = value.get("val")
        try:
            # xAI books credits as negative (a liability); magnitude is what
            # we meter.
            return abs(float(str(value or "0")))
        except (TypeError, ValueError):
            return 0.0

    core = preview.get("coreInvoice", {})
    limit = cents(preview.get("effectiveSpendingLimit"))
    prepaid_total = cents(core.get("prepaidCredits"))

    if limit > 0:
        # Postpaid: spend against the monthly spending limit.
        used, denom = cents(core.get("amountAfterVat")), limit
    elif prepaid_total > 0:
        # Prepaid: credits consumed out of credits granted this cycle.
        used, denom = cents(core.get("prepaidCreditsUsed")), prepaid_total
    else:
        # No limit and no prepaid credits — nothing to meter.
        return None, team_id

    pct = max(0.0, min(1.0, used / denom))
    reset_at = _next_month_start(datetime.now(timezone.utc))
    reset = format_epoch_countdown(reset_at)
    if log:
        log(f"[grok-usage] ${used/100:.2f}/${denom/100:.2f} ({pct:.0%})")
    return {
        "monthly_pct": pct,
        "monthly_reset": reset,
        "monthly_reset_at": reset_at,
        "spent_usd": used / 100.0,
        "limit_usd": denom / 100.0,
        "limits": [{
            "label": "Monthly",
            "pct": pct,
            "reset": reset,
            "reset_at": reset_at,
        }],
    }, team_id


class GrokAgent:
    def __init__(self, grok_home: str, management_key: str = "",
                 team_id: str = "",
                 log: Callable[[str], None] | None = None) -> None:
        self.grok_home = grok_home
        self.management_key = management_key
        self.log = log
        self._team_id = team_id

    def get_usage(self) -> dict | None:
        if not self.management_key:
            return None
        usage, self._team_id = get_usage(
            self.management_key, team_id=self._team_id, log=self.log)
        return usage

    def session_path_for_session(self, session_id: str) -> str:
        return sessions_path_for_session(self.grok_home, session_id)

    def latest_transcript_path(self) -> str:
        return latest_transcript_path(self.grok_home)


def ensure_compat_hooks_off(config_path: str,
                            log: Callable[[str], None] | None = None) -> None:
    """Disable Grok's Claude/Cursor harness-compatibility hooks.

    With harness-compat hooks enabled, Grok runs the *other* agents' hooks
    (reporting as claude/cursor) and never fires its own — so codelight never
    sees Grok's Notification event (the only "waiting" signal). Setting
    ``[compat.<h>] hooks = false`` makes Grok fire its native hooks instead.
    Idempotent; edits ~/.grok/config.toml in place, preserving other keys.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)
    try:
        import tomllib
    except Exception:
        return
    try:
        with open(config_path, "rb") as stream:
            current = tomllib.load(stream)
    except FileNotFoundError:
        current = {}
    except Exception:
        return  # unparseable — never risk corrupting the user's config
    compat = current.get("compat") or {}
    need = [h for h in ("claude", "cursor")
            if (compat.get(h) or {}).get("hooks") is not False]
    if not need:
        return
    try:
        with open(config_path) as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
        lines = []
    for harness in need:
        header = f"[compat.{harness}]"
        idx = next((i for i, ln in enumerate(lines)
                    if ln.strip() == header), -1)
        if idx == -1:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [header, "hooks = false"]
            continue
        end = next((j for j in range(idx + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))
        hk = next((j for j in range(idx + 1, end)
                   if lines[j].split("=", 1)[0].strip() == "hooks"), -1)
        if hk == -1:
            lines.insert(idx + 1, "hooks = false")
        else:
            lines[hk] = "hooks = false"
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    _log(f"[grok-hooks] disabled harness-compat hooks ({', '.join(need)}) "
         f"in {config_path}")


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
        # Grok expects matcher groups: event → [{matcher?, hooks:[{type,command}]}].
        # No matcher = match all (status events aren't tool-specific).
        return [{"hooks": [{"type": "command", "command": f"{cmd_base} {state}"}]}]

    doc = {
        "hooks": {
            # NB: no SessionStart→working. A session merely *opening* is not
            # work; Grok also spawns a leader session that fires only
            # SessionStart, so mapping it to "working" leaves a phantom
            # session stuck for IDLE_WINDOW (600s) that outranks the real
            # session's "waiting" state and hides permission prompts.
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


def build_integration(config: dict, *,
                      log: Callable[[str], None] | None = None) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.grok):
    home; management_key or management_key_file (xAI billing management key,
    or set XAI_MANAGEMENT_KEY) to enable the monthly budget meter."""
    home = (os.path.expanduser(str(config.get("home") or ""))
            or default_home())
    agent = GrokAgent(home, management_key=_management_key(config),
                      team_id=str(config.get("team_id") or ""), log=log)
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
        # Grok must fire its own hooks (not the borrowed Claude/Cursor ones)
        # for the Notification→waiting signal to reach codelight.
        ensure_compat_hooks_off(os.path.join(home, "config.toml"), log)

    return base.AgentIntegration(
        spec=SPEC,
        agent=agent,
        hook_modes=HOOK_MODES,
        # Opt-in monthly $-budget meter via the xAI Management API; only wired
        # when a management key is configured (and hidden if no limit is set).
        usage_fetcher=agent.get_usage if agent.management_key else None,
        install_hooks=_install_hooks,
        removable_files=(hooks_file,),
        removable_empty_dirs=(os.path.dirname(hooks_file),),
        transcript_path_for_session=agent.session_path_for_session,
        latest_transcript_fallback=agent.latest_transcript_path,
        transcript_extractor=transcript_extractor,
    )
