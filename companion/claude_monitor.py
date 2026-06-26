#!/usr/bin/env python3
"""
claude_monitor.py – pushes Claude Code status to GeekMagic Ultra display.

Usage:
    python claude_monitor.py [--device claude-screen.local] [--secret mysecret]
    python claude_monitor.py --dry-run            # print payload, no POST
    python claude_monitor.py --dry-run --verbose  # also show raw data sources
    python3 -u claude_monitor.py --dry-run | tee  # -u avoids buffering when piping
"""
import argparse
import glob
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

CLAUDE_SESSIONS_DIR  = os.path.expanduser("~/.claude/sessions")
CLAUDE_PROJECTS_DIR  = os.path.expanduser("~/.claude/projects")
MONITOR_STATE_DIR    = os.path.expanduser("~/.claude/monitor_state")

STATUS_INTERVAL  =  2   # seconds between POST /status calls
USAGE_INTERVAL   = 60   # seconds between usage API polls
WORKING_ACTIVITY = 15   # any JSONL write within N seconds → WORKING (type-agnostic)
WAITING_WINDOW   = 90   # seconds: recent "assistant" message → WAITING
IDLE_WINDOW      = 600  # seconds: ignore sessions with no activity in the last 10 min
WORKING_STICKY   = 5   # seconds: keep WORKING state after last detection

# Set by main() based on --verbose flag
_verbose = False

# ── Helpers ───────────────────────────────────────────────────────────────────

def vprint(*args, **kwargs):
    if _verbose:
        print(*args, **kwargs, flush=True)


def pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def find_session_jsonl(session_data: dict) -> str | None:
    """Find the JSONL history file for a session using cwd + sessionId."""
    try:
        wd  = session_data.get("cwd", "")
        sid = session_data.get("sessionId", "")
        if not wd:
            return None
        project_dir = os.path.join(CLAUDE_PROJECTS_DIR, wd.replace("/", "-"))
        if sid:
            exact = os.path.join(project_dir, f"{sid}.jsonl")
            if os.path.exists(exact):
                return exact
        # Fall back to most recently modified JSONL in the project dir
        candidates = glob.glob(os.path.join(project_dir, "*.jsonl"))
        return max(candidates, key=os.path.getmtime) if candidates else None
    except Exception:
        return None


def last_jsonl_entry(jsonl_path: str) -> dict | None:
    """Return the last non-empty line of a JSONL file as a dict."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(4096, size)
            f.seek(-chunk, 2)
            tail = f.read().decode("utf-8", errors="ignore")
        lines = [l for l in tail.splitlines() if l.strip()]
        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None


def run_hook(state: str) -> None:
    """
    Hook mode: called by Claude Code hooks via --hook STATE.
    Reads session context from stdin, writes a state file to MONITOR_STATE_DIR.
    Exits immediately — must be fast and never block Claude Code.
    """
    data = {}
    raw = ""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
    except Exception:
        pass

    session_id = (data.get("session_id")
                  or data.get("sessionId")
                  or data.get("session")
                  or "unknown")

    os.makedirs(MONITOR_STATE_DIR, exist_ok=True)
    path = os.path.join(MONITOR_STATE_DIR, f"{session_id}.json")

    if state == "ended":
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return

    try:
        with open(path, "w") as f:
            json.dump({"state": state, "time": time.time(), "session_id": session_id}, f)
    except Exception:
        pass


def install_hooks(script_path: str) -> None:
    """
    Ensure ~/.claude/settings.json has the monitor hooks pointing to this script.
    Idempotent: safe to call on every startup. Preserves all non-monitor hooks.
    Replaces stale references to the old standalone monitor_hook.py as well.
    """
    settings_path = os.path.expanduser("~/.claude/settings.json")

    settings = {}
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[hooks] warning: could not read {settings_path}: {e}", file=sys.stderr)
        return

    cmd_base = f"python3 {script_path} --hook"
    desired = {
        "PreToolUse":        f"{cmd_base} working",
        "PostToolUse":       f"{cmd_base} working",   # clears "waiting" after permission granted
        "UserPromptSubmit":  f"{cmd_base} working",
        "PermissionRequest": f"{cmd_base} waiting",   # Claude blocked, needs user decision
        "PermissionDenied":  f"{cmd_base} working",   # denied → Claude resumes, clear waiting
        "MessageDisplay":    f"{cmd_base} ended",     # response shown → clear working state
        # Stop fires for sub-agents (different session IDs), not the main session — omitted.
        "SessionEnd":        f"{cmd_base} ended",
    }

    # Each hook event entry uses {"matcher": "...", "hooks": [...]} format.
    # matcher="" matches all tools. See Claude Code settings schema.
    def is_monitor_cmd(cmd: str) -> bool:
        return ("claude_monitor" in cmd and "--hook" in cmd) or "monitor_hook.py" in cmd

    hooks = settings.get("hooks", {})
    changed = False

    for event, full_cmd in desired.items():
        existing = hooks.get(event, [])
        # Already installed with correct command inside any matcher entry?
        already = any(
            isinstance(entry, dict) and
            any(isinstance(c, dict) and c.get("command") == full_cmd
                for c in entry.get("hooks", []))
            for entry in existing
        )
        if already:
            continue
        # Strip stale monitor commands from existing matcher entries, drop now-empty ones
        cleaned = []
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            inner = [c for c in entry.get("hooks", [])
                     if not (isinstance(c, dict) and is_monitor_cmd(c.get("command", "")))]
            if inner:
                cleaned.append({**entry, "hooks": inner})
        # Append our matcher entry (matcher="" = match all events of this type)
        cleaned.append({"matcher": "", "hooks": [{"type": "command", "command": full_cmd}]})
        hooks[event] = cleaned
        changed = True

    # Remove our monitor hooks from events that are no longer in desired
    # (e.g. Stop was removed in favour of PostToolUse).
    for event in list(hooks.keys()):
        if event in desired:
            continue
        cleaned = []
        for entry in hooks[event]:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue
            inner = [c for c in entry.get("hooks", [])
                     if not (isinstance(c, dict) and is_monitor_cmd(c.get("command", "")))]
            if inner:
                cleaned.append({**entry, "hooks": inner})
        if len(cleaned) != len(hooks[event]):
            if cleaned:
                hooks[event] = cleaned
            else:
                del hooks[event]
            changed = True

    if not changed:
        vprint("[hooks] already up to date")
        return

    settings["hooks"] = hooks
    os.makedirs(os.path.dirname(os.path.abspath(settings_path)), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"[hooks] installed in {settings_path}", flush=True)


def _session_status_from_hook(session_id: str) -> str | None:
    """
    Read state written by the --hook mode (event-driven, no JSONL lag).
    Returns 'working'/'waiting' or None if unavailable/stale.
    """
    path = os.path.join(MONITOR_STATE_DIR, f"{session_id}.json")
    try:
        with open(path) as f:
            data = json.load(f)
        age = time.time() - data.get("time", 0)
        if age > IDLE_WINDOW:
            return None
        state = data.get("state", "")
        return state if state in ("working", "waiting") else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def session_status(data: dict) -> str:
    """Return 'working', 'waiting', or '' (ignore this session).
    Accepts the already-loaded session JSON dict to avoid re-reading the file."""
    session_id = data.get("sessionId", "")

    if session_id:
        hook_state = _session_status_from_hook(session_id)
        if hook_state is not None:
            vprint(f"    [hook] {session_id[:8]}… → {hook_state}")
            return hook_state

    # Fall back to JSONL analysis (for sessions started before hooks were installed,
    # or if the hooks aren't firing yet).
    jsonl = find_session_jsonl(data)
    if not jsonl:
        return ""
    entry = last_jsonl_entry(jsonl)
    if not entry:
        return ""

    ts_raw = entry.get("timestamp", "")
    if not ts_raw:
        return ""
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except ValueError:
        return ""

    if age > IDLE_WINDOW:
        return ""

    # Any JSONL write in the last WORKING_ACTIVITY seconds = working, regardless of type.
    if age < WORKING_ACTIVITY:
        return "working"

    return ""


def get_active_sessions() -> tuple[int, int, str]:
    """Return (active_count, live_count, overall_status).

    active_count — sessions with a working/waiting hook state (shown on display)
    live_count   — all live non-probe Claude processes (used for WORKING_STICKY gate)
    overall_status — 'working', 'waiting', or 'inactive'
    """
    active  = 0
    live    = 0
    overall = "inactive"

    session_files = glob.glob(os.path.join(CLAUDE_SESSIONS_DIR, "*.json"))
    vprint(f"[sessions] scanning {len(session_files)} file(s) in {CLAUDE_SESSIONS_DIR}")

    for sf in session_files:
        try:
            with open(sf) as f:
                data = json.load(f)
            pid  = data.get("pid", 0)
            name = os.path.basename(sf)

            if not pid_alive(pid):
                vprint(f"  {name}  PID {pid} (dead)  → skipped")
                continue

            cwd = data.get("cwd", "")
            if os.path.basename(cwd).startswith(".claude_probe_"):
                vprint(f"  {name}  PID {pid} (monitor probe) → skipped")
                continue

            live += 1

            st = session_status(data)
            if not st:
                vprint(f"  {name}  PID {pid} (alive, idle) → skipped")
                continue

            active += 1
            vprint(f"  {name}  PID {pid} (alive) → {st}")

            if st == "working":
                overall = "working"
            elif st == "waiting" and overall != "working":
                overall = "waiting"
        except Exception as e:
            vprint(f"  {os.path.basename(sf)}  error: {e}")
            continue

    vprint(f"[sessions] result: {active} active, {live} live, overall={overall}")
    return active, live, overall


def _format_countdown(diff_secs: int) -> str:
    if diff_secs <= 0:
        return "--"
    days  = diff_secs // 86400
    hours = (diff_secs % 86400) // 3600
    mins  = (diff_secs % 3600)  // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _format_iso_countdown(iso_ts: str) -> str:
    """Convert an ISO-8601 timestamp to a human-readable countdown like '3h 45m'."""
    if not iso_ts:
        return "--"
    try:
        target = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        diff = int((target - datetime.now(timezone.utc)).total_seconds())
        return _format_countdown(diff)
    except Exception:
        return "--"


# ── Usage via claude.ai /api/oauth/usage ─────────────────────────────────────
# Direct API call — no subprocess, no screen scraping.  Credentials are read
# fresh each poll so token rotations by Claude Code are picked up automatically.

_USAGE_API = "https://claude.ai/api/oauth/usage"
_CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")


def get_usage(weekly_limit: int = 0, daily_limit: int = 0) -> dict | None:
    """
    Fetch usage from the claude.ai OAuth usage API.
    Returns a dict with session_pct/weekly_pct/resets, or None on failure
    (caller keeps cached values).
    """
    try:
        with open(_CREDS_PATH) as f:
            creds = json.load(f)
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception as e:
        vprint(f"[usage] could not read credentials: {e}")
        return None

    req = urllib.request.Request(
        _USAGE_API,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent":    "claude-code/1.0.0",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        vprint(f"[usage] HTTP {e.code}: {e.reason}")
        return None
    except Exception as e:
        vprint(f"[usage] request error: {e}")
        return None

    session = data.get("five_hour") or {}
    weekly  = data.get("seven_day")  or {}

    session_pct   = float(session.get("utilization") or 0.0) / 100.0
    weekly_pct    = float(weekly.get("utilization")  or 0.0) / 100.0
    session_reset = _format_iso_countdown(session.get("resets_at", ""))
    weekly_reset  = _format_iso_countdown(weekly.get("resets_at",  ""))

    vprint(f"[usage] API: session={session_pct:.0%} weekly={weekly_pct:.0%}")
    return {
        "session_pct":   session_pct,
        "weekly_pct":    weekly_pct,
        "session_reset": session_reset,
        "weekly_reset":  weekly_reset,
    }


def print_payload(payload: dict, url: str) -> None:
    """Pretty-print the payload that would be sent to the device."""
    ts = datetime.now().strftime("%H:%M:%S")
    status = payload["status"]
    status_colors = {"working": "\033[33m", "waiting": "\033[31m", "inactive": "\033[32m"}
    color = status_colors.get(status, "")
    reset = "\033[0m" if color else ""

    bar_w = 30
    def bar(pct):
        filled = round(pct * bar_w)
        return "[" + "█" * filled + "░" * (bar_w - filled) + f"] {pct:.0%}"

    print(f"\n[{ts}] DRY RUN – would POST to {url}")
    print(f"  Weekly:   {bar(payload['weekly_pct'])}  resets {payload['weekly_reset']}")
    print(f"  Session:  {bar(payload['session_pct'])}  resets {payload['session_reset']}")
    print(f"  Sessions: {payload['sessions']}")
    print(f"  Status:   {color}{status.upper()}{reset}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global _verbose

    parser = argparse.ArgumentParser()
    parser.add_argument("--hook", metavar="STATE",
                        help="Hook mode: write STATE to monitor state file and exit. "
                             "Used internally by Claude Code hooks (working/waiting/ended).")
    parser.add_argument("--device", default="claude-screen.local",
                        help="Device hostname or IP (default: claude-screen.local)")
    parser.add_argument("--secret", default="",
                        help="Shared secret (X-Secret header)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Print payload instead of POSTing to device")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show raw data sources (usage text, session files)")
    parser.add_argument("--weekly-limit", type=int, default=0,
                        help="Max weekly requests for fallback progress bar (0=disable)")
    parser.add_argument("--daily-limit", type=int, default=0,
                        help="Max daily requests for fallback progress bar (0=disable)")
    args = parser.parse_args()

    # Hook mode: invoked by Claude Code hooks, not the user.
    if args.hook:
        run_hook(args.hook)
        return

    _verbose = args.verbose

    # Auto-install hooks in ~/.claude/settings.json on first run (idempotent).
    install_hooks(os.path.abspath(__file__))

    url     = f"http://{args.device}/status"
    headers = {"Content-Type": "application/json"}
    if args.secret:
        headers["X-Secret"] = args.secret


    mode = "DRY RUN" if args.dry_run else f"posting to {url}"
    print(f"claude_monitor  [{mode}]  (Ctrl-C to stop)", flush=True)

    weekly_limit = args.weekly_limit
    daily_limit  = args.daily_limit

    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    _run_loop(args, url, headers, weekly_limit, daily_limit)


def _run_loop(args, url, headers, weekly_limit, daily_limit):
    if not args.dry_run:
        try:
            import requests
        except ImportError:
            sys.exit("Install requests:  pip install requests")

    _empty = {"session_pct": 0.0, "weekly_pct": 0.0,
              "session_reset": "--", "weekly_reset": "--"}
    usage_cache  = _empty
    last_usage   = 0.0   # 0 → poll immediately on first iteration
    last_working = 0.0   # monotonic time of last WORKING detection (for sticky state)

    while True:
        loop_start = time.monotonic()

        if loop_start - last_usage >= USAGE_INTERVAL:
            last_usage = loop_start   # update first — guarantees 60s gap even on error
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [usage] polling…", flush=True)
            try:
                fresh = get_usage(weekly_limit, daily_limit)
            except Exception as e:
                print(f"[usage] unexpected error: {e}", file=sys.stderr, flush=True)
                fresh = None
            if fresh is None:
                print("[usage] no data – keeping cached values", flush=True)
            else:
                usage_cache = fresh

        sessions, live, status = get_active_sessions()

        if status == "working":
            last_working = loop_start
        elif (status == "inactive" and live > 0
              and loop_start - last_working < WORKING_STICKY):
            # Claude is still running but briefly idle between tool calls — hold WORKING.
            # When live == 0, Claude has exited and we drop to inactive immediately.
            status = "working"

        payload = {
            **usage_cache,
            "sessions": sessions,
            "status":   status,
        }

        if args.dry_run:
            print_payload(payload, url)
        else:
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=5)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"status={status} sessions={sessions} "
                      f"weekly={usage_cache['weekly_pct']:.0%} "
                      f"session={usage_cache['session_pct']:.0%}  "
                      f"→ {r.status_code}")
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] POST failed: {e}",
                      file=sys.stderr)

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0, STATUS_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
