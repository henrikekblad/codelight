from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from codelight_core import hooks as hooks_core
from codelight_core.agents import base
from codelight_core.timefmt import format_epoch_countdown


LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid" viewBox="0 0 256 208">'
    '<path fill="currentColor" d="M205.3 31.4c14 14.8 20 35.2 22.5 63.6 6.6 0 12.8 1.5 17 7.2l7.8 10.6c2.2 3 3.4 6.6 3.4 10.4v28.7a12 12 0 0 1-4.8 9.5C215.9 187.2 172.3 208 128 208c-49 0-98.2-28.3-123.2-46.6a12 12 0 0 1-4.8-9.5v-28.7c0-3.8 1.2-7.4 3.4-10.5l7.8-10.5c4.2-5.7 10.4-7.2 17-7.2 2.5-28.4 8.4-48.8 22.5-63.6C77.3 3.2 112.6 0 127.6 0h.4c14.7 0 50.4 2.9 77.3 31.4ZM128 78.7c-3 0-6.5.2-10.3.6a27.1 27.1 0 0 1-6 12.1 45 45 0 0 1-32 13c-6.8 0-13.9-1.5-19.7-5.2-5.5 1.9-10.8 4.5-11.2 11-.5 12.2-.6 24.5-.6 36.8 0 6.1 0 12.3-.2 18.5 0 3.6 2.2 6.9 5.5 8.4C79.9 185.9 105 192 128 192s48-6 74.5-18.1a9.4 9.4 0 0 0 5.5-8.4c.3-18.4 0-37-.8-55.3-.4-6.6-5.7-9.1-11.2-11-5.8 3.7-13 5.1-19.7 5.1a45 45 0 0 1-32-12.9 27.1 27.1 0 0 1-6-12.1c-3.4-.4-6.9-.5-10.3-.6Zm-27 44c5.8 0 10.5 4.6 10.5 10.4v19.2a10.4 10.4 0 0 1-20.8 0V133c0-5.8 4.6-10.4 10.4-10.4Zm53.4 0c5.8 0 10.4 4.6 10.4 10.4v19.2a10.4 10.4 0 0 1-20.8 0V133c0-5.8 4.7-10.4 10.4-10.4Zm-73-94.4c-11.2 1.1-20.6 4.8-25.4 10-10.4 11.3-8.2 40.1-2.2 46.2A31.2 31.2 0 0 0 75 91.7c6.8 0 19.6-1.5 30.1-12.2 4.7-4.5 7.5-15.7 7.2-27-.3-9.1-2.9-16.7-6.7-19.9-4.2-3.6-13.6-5.2-24.2-4.3Zm69 4.3c-3.8 3.2-6.4 10.8-6.7 19.9-.3 11.3 2.5 22.5 7.2 27a41.7 41.7 0 0 0 30 12.2c8.9 0 17-2.9 21.3-7.2 6-6.1 8.2-34.9-2.2-46.3-4.8-5-14.2-8.8-25.4-9.9-10.6-1-20 .7-24.2 4.3ZM128 56c-2.6 0-5.6.2-9 .5.4 1.7.5 3.7.7 5.7 0 1.5 0 3-.2 4.5 3.2-.3 6-.3 8.5-.3 2.6 0 5.3 0 8.5.3-.2-1.6-.2-3-.2-4.5.2-2 .3-4 .7-5.7-3.4-.3-6.4-.5-9-.5Z"/>'
    '</svg>'
)

# 48x48 1-bit render of LOGO_SVG for the ESP8266 screen.
LOGO_BITMAP = (
    "AAA/+AAAAAD//wAAAAP//8AAAAf//+AAAA////AAAB////gAAD////wAAHgf+B4AAOAP8Ac"
    "AAOAP8AcAAcAH4AOAAcAH4AOAA8AH4AOAA8AGYAPAA8AGYAPAA8AP8APAA8AP8APAB8AP8A"
    "PgB8Af+APgB+A8PAfgB/D8Pw/gB//4H//gH//wD//4P+/gB/f8f8AAAAP+f8AAAAP+/8AAA"
    "AH//4AAAAH//4AwDAH//4B4HgH//4B4HgH//4B4HgH//4B4HgH//4B4HgH//4B4HgH//4B4"
    "HgH//4B4HgH/f4AwDAH+P4AAAAH8H8AAAAP4D/AAAA/wA/wAAD/AAf8AAP+AAH/wD/4AAB/"
    "///wAAAf///AAAAH//4AAAAA//AAA"
)

SPEC = base.AgentSpec(
    "copilot",
    "Copilot",
    executables=("copilot",),
    vscode_extensions=frozenset({"github.copilot", "github.copilot-chat"}),
    color="#007FFF",
    logo_svg=LOGO_SVG,
    logo_bitmap=LOGO_BITMAP,
    trusted_auto_allow_tools=frozenset({
        "check_workspace_trust",
        "read_workspace_status",
        "list_dir",
        "read_file",
        "file_search",
        "grep_search",
        "semantic_search",
        "get_errors",
        "get_changed_files",
        "copilot_getNotebookSummary",
        "read_notebook_cell_output",
        "read_page",
        "screenshot_page",
        "view_image",
        "fetch_webpage",
        "vscode_listCodeUsages",
        "terminal_last_command",
        "terminal_selection",
        "testFailure",
        "get_task_output",
        "manage_todo_list",
    }),
)

HOOK_MODES = (
    base.HookMode("permission-copilot", kind="permission",
                  envelope=base.BEHAVIOR, default_agent_id="copilot"),
    base.HookMode("permission-vscode", kind="permission",
                  envelope=base.PRETOOL_DECISION, default_agent_id="copilot",
                  requires_tool_use_id=True),
    base.HookMode("question-vscode", kind="question",
                  envelope=base.CONTEXT, default_agent_id="copilot"),
)


def default_home() -> str:
    return os.path.expanduser(os.environ.get("COPILOT_HOME", "~/.copilot"))


def transcript_extractor(record: dict, tool_summary) -> tuple[str, object] | None:
    """Match Copilot's events JSONL: {"type": "user.message"|"assistant.message", "data": ...}."""
    t = str(record.get("type") or "").strip().lower()
    if t not in ("user.message", "assistant.message"):
        return None
    data = record.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        if content is not None:
            return ("user" if t.startswith("user") else "assistant"), content
    return None


def build_integration(
    config: dict,
    *,
    api: Callable[[str, str], dict] | None = None,
    log: Callable[[str], None] | None = None,
) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.copilot):
    home, github_org, github_token_file.

    Copilot usage is the organization's pooled monthly AI-credit billing;
    without github_org (plus a token via CODELIGHT_GITHUB_TOKEN/GITHUB_TOKEN/
    GH_TOKEN, github_token_file, or the gh CLI) no usage is reported.
    """
    home = (os.path.expanduser(str(config.get("home") or ""))
            or default_home())
    agent = CopilotAgent(
        str(config.get("github_org") or ""),
        copilot_home=home,
        token_file=str(config.get("github_token_file") or ""),
        api=api,
        log=log,
    )
    hooks_file = hooks_path(home)

    def _install_hooks(*, script_path, hook_wait_ceiling, remote_permissions,
                       remote_questions, permission_timeout, log=None):
        # Copilot has no question hook of its own; remote_questions rides along
        # on the PreToolUse question-vscode hook installed with permissions.
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
        usage_fetcher=agent.get_usage,
        install_hooks=_install_hooks,
        removable_files=(hooks_file,),
        removable_empty_dirs=(os.path.dirname(hooks_file),),
        transcript_path_for_session=agent.events_path_for_session,
        # Copilot hooks do not always pass a transcript path, so keep the old
        # behavior of falling back to its newest local events file.
        latest_transcript_fallback=agent.latest_events_path,
        transcript_extractor=transcript_extractor,
    )


def events_path_for_session(copilot_home: str, session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    base = os.path.realpath(os.path.join(copilot_home, "session-state"))
    path = os.path.realpath(os.path.join(base, sid, "events.jsonl"))
    if not path.startswith(base + os.sep):
        return ""
    return path if os.path.isfile(path) else ""


def latest_events_path(copilot_home: str) -> str:
    try:
        base = os.path.join(copilot_home, "session-state")
        newest_path = ""
        newest_mtime = 0.0
        for root, _, files in os.walk(base):
            if "events.jsonl" not in files:
                continue
            path = os.path.join(root, "events.jsonl")
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


def hooks_path(copilot_home: str) -> str:
    return os.path.join(copilot_home, "hooks", "codelight.json")


class CopilotAgent:
    def __init__(
        self,
        org: str = "",
        *,
        copilot_home: str = "",
        token_file: str = "",
        api: Callable[[str, str], dict] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.org = org
        self.copilot_home = copilot_home
        self.token_file = token_file
        self.api = api or github_api
        self.log = log

    def token(self) -> str:
        return github_token(self.token_file)

    def get_usage(
        self,
        *,
        org: str | None = None,
        token: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        resolved_org = (org if org is not None else self.org).strip()
        resolved_token = token if token is not None else self.token()
        if not resolved_org or not resolved_token:
            return None
        return get_usage(
            resolved_org,
            resolved_token,
            now or datetime.now(timezone.utc),
            api=self.api,
            log=self.log,
        )

    def events_path_for_session(self, session_id: str) -> str:
        return events_path_for_session(self.copilot_home, session_id)

    def latest_events_path(self) -> str:
        return latest_events_path(self.copilot_home)


def github_token(token_file: str = "") -> str:
    """Resolve a GitHub token without making the gh CLI a requirement."""
    for key in ("CODELIGHT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(key, "").strip()
        if token:
            return token
    if token_file:
        try:
            with open(os.path.expanduser(token_file)) as stream:
                return stream.read().strip()
        except Exception:
            pass
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return ""


def github_api(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "codelight",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def next_month_start(now: datetime) -> int:
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


def get_usage(
    org: str,
    token: str,
    now: datetime,
    *,
    api: Callable[[str, str], dict] = github_api,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Fetch the organization's pooled monthly Copilot AI-credit usage."""
    org = org.strip()
    if not org or not token:
        return None
    query = urllib.parse.urlencode({"year": now.year, "month": now.month})
    try:
        billing = api(
            f"organizations/{urllib.parse.quote(org)}/settings/billing/"
            f"ai_credit/usage?{query}", token)
    except urllib.error.HTTPError as e:
        if log:
            log(f"[copilot-usage] billing unavailable for {org}: HTTP {e.code}")
        return None
    except Exception as e:
        if log:
            log(f"[copilot-usage] billing request failed: {e}")
        return None
    try:
        subscription = api(
            f"orgs/{urllib.parse.quote(org)}/copilot/billing", token)
    except urllib.error.HTTPError as e:
        if log:
            log(f"[copilot-usage] subscription unavailable for {org}: HTTP {e.code}")
        return None
    except Exception as e:
        if log:
            log(f"[copilot-usage] subscription request failed: {e}")
        return None

    try:
        used = sum(
            float(item.get("grossQuantity") or 0.0)
            for item in billing.get("usageItems", [])
            if item.get("product") == "Copilot"
            and str(item.get("unitType", "")).lower() in
            {"credits", "ai-credits"}
        )
        seats = int(subscription.get("seat_breakdown", {}).get("total") or 0)
        plan = str(subscription.get("plan_type") or "business").lower()
    except (TypeError, ValueError):
        return None
    if seats <= 0:
        return None

    promotional = datetime(2026, 6, 1, tzinfo=timezone.utc) <= now < \
        datetime(2026, 9, 1, tzinfo=timezone.utc)
    per_seat = (
        7000 if promotional and plan == "enterprise"
        else 3000 if promotional
        else 3900 if plan == "enterprise"
        else 1900
    )
    allowance = seats * per_seat
    reset_at = next_month_start(now)
    pct = max(0.0, min(1.0, used / allowance))
    if log:
        log(f"[copilot-usage] {org}: {used:.0f}/{allowance} credits ({pct:.0%})")
    return {
        "monthly_pct": pct,
        "monthly_reset": format_epoch_countdown(reset_at),
        "monthly_reset_at": reset_at,
        "used_credits": used,
        "included_credits": allowance,
        "plan_type": plan,
        "seat_count": seats,
        "limits": [{
            "label": "Monthly",
            "pct": pct,
            "reset": format_epoch_countdown(reset_at),
            "reset_at": reset_at,
        }],
    }


def install_hooks(
    hooks_file: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    permission_timeout: int = 60,
) -> None:
    cmd_base = hooks_core.hook_command_base(script_path, "copilot")

    permission_hook = {
        "type": "command",
        "command": f"{cmd_base} permission-copilot --permission-timeout {permission_timeout}",
        "timeoutSec": hook_wait_ceiling + 15,
    } if remote_permissions else {
        "type": "command",
        "command": f"{cmd_base} waiting",
    }

    doc = {
        "version": 1,
        "hooks": {
            "SessionStart": [
                {"type": "command", "command": f"{cmd_base} working"},
            ],
            "UserPromptSubmit": [
                {"type": "command", "command": f"{cmd_base} working"},
            ],
            "PreToolUse": (
                [
                    {"type": "command", "command": f"{cmd_base} working"},
                    {
                        "type": "command",
                        "command": f"{cmd_base} permission-vscode --permission-timeout {permission_timeout}",
                        "timeoutSec": hook_wait_ceiling + 15,
                    },
                    {
                        "type": "command",
                        "command": f"{cmd_base} question-vscode --permission-timeout {permission_timeout}",
                        "timeoutSec": hook_wait_ceiling + 15,
                    },
                ] if remote_permissions else [
                    {"type": "command", "command": f"{cmd_base} working"},
                ]
            ),
            "PostToolUse": [
                {"type": "command", "command": f"{cmd_base} working"},
            ],
            "PermissionRequest": [
                permission_hook,
            ],
            "Notification": [
                {
                    "type": "command",
                    "matcher": "permission_prompt|elicitation_dialog|agent_idle",
                    "command": f"{cmd_base} waiting",
                },
            ],
            "Stop": [
                {"type": "command", "command": f"{cmd_base} ended"},
            ],
            "SessionEnd": [
                {"type": "command", "command": f"{cmd_base} ended"},
            ],
        },
    }

    try:
        with open(hooks_file) as stream:
            existing = json.load(stream)
    except Exception:
        existing = {}
    if existing == doc:
        print(f"[copilot-hooks] already up to date in {hooks_file}", flush=True)
        return
    hooks_core.write_json_object(hooks_file, doc)
    print(f"[copilot-hooks] installed in {hooks_file}", flush=True)
