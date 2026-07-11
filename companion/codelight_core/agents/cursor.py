from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from codelight_core import hooks as hooks_core
from codelight_core.agents import base
from codelight_core.timefmt import epoch, format_iso_countdown


USAGE_API = "https://cursor.com/api/usage-summary"


def default_state_db() -> str:
    """Cursor IDE's SQLite state store (holds the auth token). Linux path;
    override via agents.cursor.state_db on other platforms."""
    return os.path.expanduser(
        "~/.config/Cursor/User/globalStorage/state.vscdb")


# Cursor wraps the user's prompt in <timestamp>…</timestamp><user_query>…
# </user_query> and stores redacted reasoning as a bare "[REDACTED]" text
# block — strip both so the conversation feed shows just the real text.
_TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>", re.DOTALL)
_USER_QUERY_TAG_RE = re.compile(r"</?user_query>")
# Cursor appends a bare "[REDACTED]" reasoning marker — as its own block or
# trailing the real text — that we drop from the feed.
_REDACTED_TRAILING_RE = re.compile(r"\s*\[REDACTED\]\s*$")


def _clean_text(text: str) -> str:
    text = _TIMESTAMP_RE.sub("", text)
    text = _USER_QUERY_TAG_RE.sub("", text)
    text = _REDACTED_TRAILING_RE.sub("", text)
    return text.strip()


def _clean_content(content):
    if isinstance(content, str):
        return _clean_text(content)
    if not isinstance(content, list):
        return content
    cleaned = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = _clean_text(str(block.get("text") or ""))
            if not text:
                continue
            cleaned.append({**block, "text": text})
        else:
            cleaned.append(block)
    return cleaned


# Cursor's cube mark (lobe-icons), fills with currentColor.
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" fill-rule="evenodd" d="M22.106 5.68L12.5.135a'
    '.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577'
    '.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727'
    'V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.06'
    '4-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.'
    '062-.228h18.55c.264 0 .428.286.296.514z"/></svg>'
)

# 48x48 1-bit render of LOGO_SVG for the ESP8266 screen.
LOGO_BITMAP = (
    "AAABgAAAAAAH4AAAAAAf+AAAAAB//gAAAAD//wAAAAP//8AAAA////AAAD////wAAH////4A"
    "Af////+AB//////gD//////wH//////4HAAAAAAYHwAAAAAYH4AAAAA4H+AAAAB4H/gAAAB4"
    "H/4AAAD4H/8AAAD4H//AAAH4H//wAAH4H//4AAP4H//+AAf4H///AAf4H///AA/4H///AA/4"
    "H///AB/4H///AD/4H///AD/4H///AH/4H///AH/4H///AP/4H///AP/4H///Af/4H///A//4"
    "D///A//wB///B//gAf//B/+AAH//D/4AAB//D/wAAA//H/AAAAP/P8AAAAD/PwAAAAB/fgAA"
    "AAAfeAAAAAAH4AAAAAABgAAA"
)

SPEC = base.AgentSpec(
    "cursor",
    "Cursor",
    # The IDE binary is `cursor`; the CLI installs `cursor-agent` (aliased
    # `agent`, deliberately not probed — the name is too generic).
    executables=("cursor", "cursor-agent"),
    color="#FFFFFF",
    logo_svg=LOGO_SVG,
    logo_bitmap=LOGO_BITMAP,
)

HOOK_MODES = (
    # beforeShellExecution/beforeMCPExecution accept a real three-way answer:
    # allow bypasses Cursor's prompt, deny blocks, and "ask" explicitly falls
    # back to Cursor's own prompt — used when no remote decision arrives.
    base.HookMode("permission-cursor", kind="permission",
                  envelope=base.CURSOR_PERMISSION, default_agent_id="cursor",
                  fallback_decision="ask"),
)


def default_home() -> str:
    return os.path.expanduser(os.environ.get("CURSOR_HOME", "~/.cursor"))


def hooks_path(cursor_home: str) -> str:
    # Cursor's user-level hook file — shared with the user's own hooks, so
    # install/uninstall must merge (flat entries), never overwrite.
    return os.path.join(cursor_home, "hooks.json")


def transcript_path_for_session(cursor_home: str, session_id: str) -> str:
    """Resolve Cursor's agent-transcript JSONL for a conversation id.

    Cursor stores them at
    ``~/.cursor/projects/<project>/agent-transcripts/<id>/<id>.jsonl``.
    Used when a status hook omits ``transcript_path`` (only the
    conversation_id is guaranteed).
    """
    sid = str(session_id or "").strip()
    if not sid or sid == "unknown":
        return ""
    try:
        base = os.path.realpath(os.path.join(cursor_home, "projects"))
        for root, _, files in os.walk(base):
            if os.path.basename(os.path.dirname(root)) != "agent-transcripts":
                continue
            for name in files:
                if sid in name and name.endswith(".jsonl"):
                    path = os.path.realpath(os.path.join(root, name))
                    if path.startswith(base + os.sep) and os.path.isfile(path):
                        return path
    except Exception:
        pass
    return ""


def latest_transcript_path(cursor_home: str) -> str:
    """Newest agent-transcript JSONL across all projects — lets a client
    request Cursor's latest conversation before any hook fires this run."""
    try:
        base = os.path.realpath(os.path.join(cursor_home, "projects"))
        newest_path = ""
        newest_mtime = 0.0
        for root, _, files in os.walk(base):
            if os.path.basename(os.path.dirname(root)) != "agent-transcripts":
                continue
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
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
    """Cursor's agent-transcript JSONL: top-level ``role`` with the content
    blocks nested under ``message`` (same block shapes as Claude Code)."""
    role = str(record.get("role") or "").strip().lower()
    if role not in ("user", "assistant"):
        return None
    message = record.get("message")
    if isinstance(message, dict) and message.get("content") is not None:
        return role, _clean_content(message["content"])
    return None


def _access_token(state_db: str) -> str:
    """Read Cursor's session JWT from its local SQLite store (read-only)."""
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return ""
    value = row[0] if row else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value or "").strip().strip('"')


def get_usage(
    state_db: str,
    *,
    usage_api: str = USAGE_API,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Cursor's monthly included-usage percentage from its web API, authed with
    the token in the local IDE store. Returns None (meter hidden) on any issue —
    not logged in, token expired, endpoint changed, offline."""
    token = _access_token(state_db)
    if not token or token.count(".") != 2:
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(
                token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)))
        workos_id = str(payload.get("sub") or "").split("|")[-1]
    except Exception:
        return None
    if not workos_id:
        return None

    cookie = ("WorkosCursorSessionToken="
              + urllib.parse.quote(workos_id) + "%3A%3A"
              + urllib.parse.quote(token))
    req = urllib.request.Request(usage_api, headers={
        "Cookie": cookie, "Accept": "*/*", "User-Agent": "codelight"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        if log:
            log(f"[cursor-usage] HTTP {e.code}: {e.reason}")
        return None
    except Exception as e:
        if log:
            log(f"[cursor-usage] request failed: {e}")
        return None

    plan = data.get("individualUsage", {}).get("plan", {})
    if not isinstance(plan, dict):
        return None
    try:
        pct = max(0.0, min(1.0, float(plan.get("totalPercentUsed") or 0.0) / 100.0))
    except (TypeError, ValueError):
        return None
    cycle_end = str(data.get("billingCycleEnd") or "")
    reset_at = epoch(cycle_end)
    reset = format_iso_countdown(cycle_end)
    if log:
        log(f"[cursor-usage] {data.get('membershipType','?')}: {pct:.0%}")
    return {
        "monthly_pct": pct,
        "monthly_reset": reset,
        "monthly_reset_at": reset_at,
        "membership_type": str(data.get("membershipType") or ""),
        "limits": [{
            "label": "Monthly",
            "pct": pct,
            "reset": reset,
            "reset_at": reset_at,
        }],
    }


class CursorAgent:
    def __init__(self, cursor_home: str, state_db: str,
                 log: Callable[[str], None] | None = None) -> None:
        self.cursor_home = cursor_home
        self.state_db = state_db
        self.log = log

    def get_usage(self) -> dict | None:
        return get_usage(self.state_db, log=self.log)

    def transcript_path_for_session(self, session_id: str) -> str:
        return transcript_path_for_session(self.cursor_home, session_id)

    def latest_transcript_path(self) -> str:
        return latest_transcript_path(self.cursor_home)


def install_hooks(
    hooks_file: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    permission_timeout: int = 60,
    vprint: Callable[[str], None] | None = None,
) -> None:
    cmd_base = hooks_core.hook_command_base(script_path, "cursor")

    def entry(state: str) -> list[dict]:
        return [{"command": f"{cmd_base} {state}"}]

    desired: dict[str, list[dict]] = {
        # Every hook payload carries transcript_path, so status events double
        # as the conversation-feed source.
        "sessionStart":       entry("working"),
        "beforeSubmitPrompt": entry("working"),
        "postToolUse":        entry("working"),
        "postToolUseFailure": entry("working"),
        "stop":               entry("ended"),
        "sessionEnd":         entry("ended"),
    }
    if remote_permissions:
        permission = {
            "command": f"{cmd_base} permission-cursor "
                       f"--permission-timeout {permission_timeout}",
            "timeout": hook_wait_ceiling + 15,
        }
        desired["beforeShellExecution"] = [permission]
        desired["beforeMCPExecution"] = [dict(permission)]

    hooks_core.install_flat_hooks(
        hooks_file, desired, "cursor-hooks",
        defaults={"version": 1}, vprint=vprint)


def build_integration(config: dict, *,
                      log: Callable[[str], None] | None = None) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.cursor):
    home, state_db (auth store for the usage meter), usage (default true)."""
    home = (os.path.expanduser(str(config.get("home") or ""))
            or default_home())
    state_db = (os.path.expanduser(str(config.get("state_db") or ""))
                or default_state_db())
    usage_enabled = bool(config.get("usage", True))
    agent = CursorAgent(home, state_db, log=log)
    hooks_file = hooks_path(home)

    def _install_hooks(*, script_path, hook_wait_ceiling, remote_permissions,
                       remote_questions, permission_timeout, log=None):
        install_hooks(
            hooks_file,
            script_path,
            hook_wait_ceiling=hook_wait_ceiling,
            remote_permissions=remote_permissions,
            permission_timeout=permission_timeout,
            vprint=log,
        )

    return base.AgentIntegration(
        spec=SPEC,
        agent=agent,
        hook_modes=HOOK_MODES,
        # Monthly included-usage % from Cursor's web API, authed with the
        # token in the local IDE store (no cookie paste). Degrades to no meter.
        usage_fetcher=agent.get_usage if usage_enabled else None,
        install_hooks=_install_hooks,
        # Merged into the user's own hooks.json: strip on uninstall, never delete.
        removable_hook_paths=(hooks_file,),
        transcript_path_for_session=agent.transcript_path_for_session,
        latest_transcript_fallback=agent.latest_transcript_path,
        transcript_extractor=transcript_extractor,
    )
