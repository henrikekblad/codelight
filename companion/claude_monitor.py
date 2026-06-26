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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────

CLAUDE_SESSIONS_DIR  = os.path.expanduser("~/.claude/sessions")
CLAUDE_PROJECTS_DIR  = os.path.expanduser("~/.claude/projects")
MONITOR_STATE_DIR    = os.path.expanduser("~/.claude/monitor_state")

STATUS_INTERVAL  =  2   # seconds between POST /status calls (low cost, fast detection)
USAGE_INTERVAL   = 60   # seconds between `claude /usage` calls (tmux spawn takes ~7s)
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


def parse_reset_time(text: str) -> str:
    """
    Parse a reset time string into a countdown like '2h 15m' or '3d 4h'.
    Handles:
      "Jun 27, 5pm"     – date + hour only (TUI interactive format)
      "Jun 24, 3:20pm"  – date + time
      "2:30pm"          – time only, assumes today (session reset within 24h)
    Trailing timezone like "(Europe/Stockholm)" is ignored.
    """
    now = datetime.now()

    # Format: "Month day, hour[:min]am/pm"
    m = re.search(r"(\w+ \d+),\s*(\d+)(?::(\d+))?\s*(am|pm)", text, re.IGNORECASE)
    if m:
        month_day = m.group(1)
        hour      = m.group(2)
        minute    = m.group(3) or "00"
        ampm      = m.group(4).lower()
        try:
            target = datetime.strptime(
                f"{month_day} {now.year} {hour}:{minute}{ampm}", "%b %d %Y %I:%M%p")
            if target < now:
                target = target.replace(year=now.year + 1)
            return _format_countdown(int((target - now).total_seconds()))
        except Exception:
            return "--"

    # Format: "hour[:min]am/pm"  (session reset, same day or next day)
    m = re.search(r"(\d+)(?::(\d+))?\s*(am|pm)", text, re.IGNORECASE)
    if m:
        hour   = m.group(1)
        minute = m.group(2) or "00"
        ampm   = m.group(3).lower()
        try:
            target = datetime.strptime(
                f"{now.strftime('%b %d %Y')} {hour}:{minute}{ampm}", "%b %d %Y %I:%M%p")
            if target < now:
                target += timedelta(days=1)
            return _format_countdown(int((target - now).total_seconds()))
        except Exception:
            return "--"

    return "--"


def _parse_usage_text(text: str, weekly_limit: int, daily_limit: int) -> dict | None:
    """Parse /usage output into a payload dict. Returns None if nothing useful is found."""
    # TUI format (tmux):    "43% used" on bar line, "Resets 2:30pm" on next line
    # Old inline format:    "Current session: 43% used · resets Jun 24, 3:20pm"
    pct_matches   = re.findall(r"(\d+)%\s+used", text)
    reset_matches = re.findall(r"[Rr]esets\s+([^\n(]+)", text)

    if pct_matches:
        # session is listed before weekly in the TUI output
        session_pct   = int(pct_matches[0]) / 100.0
        weekly_pct    = int(pct_matches[1]) / 100.0 if len(pct_matches) > 1 else 0.0
        session_reset = parse_reset_time(reset_matches[0]) if reset_matches else "--"
        weekly_reset  = parse_reset_time(reset_matches[1]) if len(reset_matches) > 1 else "--"
        vprint(f"[usage] pct mode: weekly={weekly_pct:.0%} session={session_pct:.0%}")
        return {
            "session_pct":   session_pct,
            "weekly_pct":    weekly_pct,
            "session_reset": session_reset,
            "weekly_reset":  weekly_reset,
        }

    wm2 = re.search(r"Last 7d\s*·\s*(\d+)\s*requests", text)
    dm2 = re.search(r"Last 24h\s*·\s*(\d+)\s*requests", text)
    if not wm2 and not dm2:
        vprint("[usage] no parseable usage patterns in screen capture")
        return None

    weekly_req  = int(wm2.group(1)) if wm2 else 0
    daily_req   = int(dm2.group(1)) if dm2 else 0
    weekly_pct  = min(1.0, weekly_req  / weekly_limit)  if weekly_limit  else 0.0
    session_pct = min(1.0, daily_req   / daily_limit)   if daily_limit   else 0.0
    vprint(f"[usage] req mode: 7d={weekly_req} 24h={daily_req}  "
           f"limits={weekly_limit}/{daily_limit}")
    return {
        "session_pct":   session_pct,
        "weekly_pct":    weekly_pct,
        "session_reset": "--",
        "weekly_reset":  "--",
    }


class _RateLimited(Exception):
    pass


# ── Persistent tmux session for /usage queries ────────────────────────────────
# One long-lived session started at monitor boot; reused for every poll.
# Avoids the per-startup auth call that triggers rate limiting.

_usage_session:   str | None = None
_usage_probe_cwd: str | None = None


def _start_usage_session() -> bool:
    """
    Start the persistent tmux session used for /usage queries.
    Handles the folder-trust dialog once at startup.
    Returns True if the session is ready, False if tmux is unavailable.
    """
    global _usage_session, _usage_probe_cwd

    if not shutil.which("tmux"):
        return False

    session   = f"_claude_usage_{os.getpid()}"
    probe_cwd = tempfile.mkdtemp(prefix=".claude_probe_")

    try:
        env = {k: v for k, v in os.environ.items()
               if k != "CLAUDE_CODE_CHILD_SESSION"}

        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session,
             "-x", "200", "-y", "50", "-c", probe_cwd, "claude"],
            env=env, check=True, timeout=10, capture_output=True,
        )

        # Wait for the prompt (up to 20s), dismissing the trust dialog if it appears.
        trust_dismissed = False
        for _ in range(40):
            time.sleep(0.5)
            r = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                               capture_output=True, text=True, timeout=5)
            if "? for shortcuts" in r.stdout:
                break
            if not trust_dismissed and (
                    "trust this folder" in r.stdout.lower()
                    or "Enter to confirm" in r.stdout):
                subprocess.run(["tmux", "send-keys", "-t", session, "", "Enter"],
                               capture_output=True, timeout=3)
                trust_dismissed = True

        _usage_session   = session
        _usage_probe_cwd = probe_cwd
        vprint(f"[usage] persistent session ready ({session})")
        return True

    except Exception as e:
        print(f"[usage] failed to start session: {e}", file=sys.stderr)
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        shutil.rmtree(probe_cwd, ignore_errors=True)
        return False


def _stop_usage_session() -> None:
    """Kill the persistent tmux session and clean up its temp dir."""
    global _usage_session, _usage_probe_cwd
    if _usage_session:
        subprocess.run(["tmux", "kill-session", "-t", _usage_session],
                       capture_output=True, timeout=3)
        _usage_session = None
    if _usage_probe_cwd:
        shutil.rmtree(_usage_probe_cwd, ignore_errors=True)
        _usage_probe_cwd = None


def _session_alive(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


def _get_usage_pty(weekly_limit: int, daily_limit: int) -> dict:
    """
    Query /usage in the persistent tmux session.
    Raises _RateLimited if the endpoint is rate limited.
    Returns None if the session is unavailable.
    """
    global _usage_session, _usage_probe_cwd

    # Restart session if it died unexpectedly.
    if not _usage_session or not _session_alive(_usage_session):
        _usage_session = None
        if not _start_usage_session():
            return None

    session = _usage_session

    try:
        # Dismiss any leftover panel, then wait until "% used" is actually gone
        # from the screen before snapshotting. A fixed sleep is not enough —
        # if the TUI is slow the panel is still visible, `before` contains the
        # old percentage, and when /usage re-renders the same value the screen
        # looks identical → screen == before → poll loop never breaks.
        subprocess.run(["tmux", "send-keys", "-t", session, "Escape", ""],
                       capture_output=True, timeout=3)
        before = ""
        for _ in range(10):
            time.sleep(0.2)
            r = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                               capture_output=True, text=True, timeout=5)
            before = r.stdout
            if "% used" not in before:
                break

        subprocess.run(["tmux", "send-keys", "-t", session, "/usage", "Enter"],
                       check=True, timeout=3, capture_output=True)

        # Poll until "% used" appears. The `before` snapshot is now guaranteed
        # panel-free, so any "% used" we see is freshly rendered.
        screen = ""
        for _ in range(20):
            time.sleep(0.5)
            r = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                               capture_output=True, text=True, timeout=5)
            screen = r.stdout
            if "% used" in screen or "rate limited" in screen.lower():
                break

        # If rate limited, use Claude's own "r to retry" up to 3 times.
        for attempt in range(3):
            if "% used" in screen or "rate limited" not in screen.lower():
                break
            vprint(f"[usage] rate limited, retrying ({attempt + 1}/3)…")
            subprocess.run(["tmux", "send-keys", "-t", session, "r", ""],
                           capture_output=True, timeout=3)
            for _ in range(10):
                time.sleep(1)
                r = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                                   capture_output=True, text=True, timeout=5)
                screen = r.stdout
                if "% used" in screen or "rate limited" in screen.lower():
                    break

        # Dismiss the usage panel so the next query starts from a clean prompt.
        subprocess.run(["tmux", "send-keys", "-t", session, "Escape", ""],
                       capture_output=True, timeout=3)

        vprint("[usage] tmux screen capture:")
        for line in screen.splitlines():
            if line.strip():
                vprint(f"  {line.rstrip()}")

        if "rate limited" in screen.lower() and "% used" not in screen:
            raise _RateLimited()

        return _parse_usage_text(screen, weekly_limit, daily_limit)

    except _RateLimited:
        raise
    except Exception as e:
        print(f"[usage] tmux error: {e}", file=sys.stderr)
        return None


def get_usage(weekly_limit: int = 0, daily_limit: int = 0) -> dict | None:
    """
    Get /usage data. Returns a dict on success, or None if rate-limited
    (caller should keep using the last cached values).
    """
    try:
        return _get_usage_pty(weekly_limit, daily_limit)
    except _RateLimited:
        vprint("[usage] rate limited – returning None to preserve cached values")
        return None


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

    _start_usage_session()
    try:
        _run_loop(args, url, headers, weekly_limit, daily_limit)
    finally:
        _stop_usage_session()


def _run_loop(args, url, headers, weekly_limit, daily_limit):
    if not args.dry_run:
        try:
            import requests
        except ImportError:
            sys.exit("Install requests:  pip install requests")

    _empty = {"session_pct": 0.0, "weekly_pct": 0.0,
              "session_reset": "--", "weekly_reset": "--"}
    usage_cache = _empty
    # Defer the first /usage call by 10s so the persistent session fully initialises
    # before hitting the usage endpoint (avoids spurious rate-limit on first query).
    last_usage  = time.monotonic() - USAGE_INTERVAL + 10
    last_working = 0.0  # monotonic time of last WORKING detection (for sticky state)

    while True:
        loop_start = time.monotonic()

        if loop_start - last_usage >= USAGE_INTERVAL:
            fresh = get_usage(weekly_limit, daily_limit)
            if fresh is None:
                vprint("[usage] no data – keeping cached values")
            else:
                usage_cache = fresh
            last_usage = loop_start

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
