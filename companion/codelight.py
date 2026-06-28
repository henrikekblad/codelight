#!/usr/bin/env python3
"""
codelight.py – pushes Claude Code status to GeekMagic Ultra display.

Usage:
    python3 codelight.py --device claude-screen.local
    python3 codelight.py --dry-run            # print payload, no POST
    python3 codelight.py --dry-run --verbose  # also show socket events and API data
    python3 -u codelight.py | tee             # -u avoids buffering when piping
"""
import argparse
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    import requests as _requests
except ImportError:
    _requests = None

# ── Config ────────────────────────────────────────────────────────────────────

MONITOR_STATE_DIR = os.path.expanduser("~/.claude/monitor_state")
SOCKET_PATH       = os.path.expanduser("~/.claude/codelight.sock")

USAGE_INTERVAL = 60    # seconds between usage API polls
IDLE_WINDOW    = 600   # seconds before a silent session is dropped from state

# ── Module-level state ────────────────────────────────────────────────────────

_verbose  = False
_shutdown = threading.Event()

_lock: threading.Lock = threading.Lock()
# session_id → {"state": "working"|"waiting", "time": float}
_sessions: dict[str, dict] = {}
_usage_cache: dict = {
    "session_pct": 0.0, "weekly_pct": 0.0,
    "session_reset": "--", "weekly_reset": "--",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def vprint(*args, **kwargs):
    if _verbose:
        print(*args, **kwargs, flush=True)


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

# ── Session state ─────────────────────────────────────────────────────────────

def _update_session(session_id: str, state: str) -> None:
    with _lock:
        if state == "ended":
            _sessions.pop(session_id, None)
        else:
            _sessions[session_id] = {"state": state, "time": time.time()}


def _overall_status() -> tuple[int, str]:
    """Return (active_count, overall_status) from in-memory session state.
    Cleans up sessions that have been silent longer than IDLE_WINDOW."""
    now = time.time()
    active  = 0
    overall = "inactive"
    with _lock:
        stale = [sid for sid, info in _sessions.items()
                 if now - info["time"] > IDLE_WINDOW]
        for sid in stale:
            del _sessions[sid]
        for info in _sessions.values():
            active += 1
            if info["state"] == "working":
                overall = "working"
            elif info["state"] == "waiting" and overall != "working":
                overall = "waiting"
    return active, overall

# ── Hook installation ─────────────────────────────────────────────────────────

def install_hooks(script_path: str) -> None:
    """
    Ensure ~/.claude/settings.json has the monitor hooks pointing to this script.
    Idempotent: safe to call on every startup. Preserves all non-monitor hooks.
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
        "SessionEnd":        f"{cmd_base} ended",
    }

    def is_monitor_cmd(cmd: str) -> bool:
        return ("codelight" in cmd and "--hook" in cmd) \
               or "monitor_hook.py" in cmd

    hooks = settings.get("hooks", {})
    changed = False

    for event, full_cmd in desired.items():
        existing = hooks.get(event, [])
        already = any(
            isinstance(entry, dict) and
            any(isinstance(c, dict) and c.get("command") == full_cmd
                for c in entry.get("hooks", []))
            for entry in existing
        )
        if already:
            continue
        cleaned = []
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            inner = [c for c in entry.get("hooks", [])
                     if not (isinstance(c, dict) and is_monitor_cmd(c.get("command", "")))]
            if inner:
                cleaned.append({**entry, "hooks": inner})
        cleaned.append({"matcher": "", "hooks": [{"type": "command", "command": full_cmd}]})
        hooks[event] = cleaned
        changed = True

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

# ── Hook mode ─────────────────────────────────────────────────────────────────

def run_hook(state: str) -> None:
    """
    Hook mode: invoked by Claude Code hooks via --hook STATE.
    Fast path: sends event to the running daemon over the Unix socket (~1 ms).
    Fallback: writes a state file if the daemon is not running.
    Must exit immediately — must never block Claude Code.
    """
    raw = ""
    data = {}
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

    # Fast path: daemon is running
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps({"state": state, "session_id": session_id}).encode())
        sock.close()
        return
    except Exception:
        pass

    # Fallback: write state file (daemon not running)
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

# ── Usage API ─────────────────────────────────────────────────────────────────
# Credentials are read fresh each poll so token rotations are picked up automatically.

_USAGE_API  = "https://claude.ai/api/oauth/usage"
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

# ── Device payload ────────────────────────────────────────────────────────────

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


def _post_to_device(url: str, headers: dict, dry_run: bool) -> None:
    """Build payload from current state and POST it to the device."""
    sessions, status = _overall_status()
    with _lock:
        usage = dict(_usage_cache)
    payload = {**usage, "sessions": sessions, "status": status}

    if dry_run:
        print_payload(payload, url)
        return

    try:
        r = _requests.post(url, json=payload, headers=headers, timeout=5)
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] status={status} sessions={sessions} "
              f"weekly={usage['weekly_pct']:.0%} "
              f"session={usage['session_pct']:.0%}  "
              f"→ {r.status_code}", flush=True)
    except Exception as e:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] POST failed: {e}", file=sys.stderr, flush=True)

# ── Daemon threads ────────────────────────────────────────────────────────────

def _socket_thread(url: str, headers: dict, dry_run: bool) -> None:
    """Accept hook events on the Unix socket and POST to the device immediately."""
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    srv.listen(32)
    srv.settimeout(1.0)
    vprint(f"[socket] listening on {SOCKET_PATH}")

    try:
        while not _shutdown.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            try:
                raw   = conn.recv(4096).decode()
                msg   = json.loads(raw)
                sid   = msg.get("session_id", "unknown")
                state = msg.get("state", "")
                if state:
                    _update_session(sid, state)
                    vprint(f"[socket] {sid[:8]}… → {state}")
                    _post_to_device(url, headers, dry_run)
            except Exception as e:
                vprint(f"[socket] error: {e}")
            finally:
                conn.close()
    finally:
        srv.close()
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass


def _usage_thread(url: str, headers: dict, dry_run: bool,
                  weekly_limit: int, daily_limit: int) -> None:
    """Poll the usage API every USAGE_INTERVAL seconds and POST after each update."""
    while not _shutdown.is_set():
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [usage] polling…", flush=True)
        try:
            fresh = get_usage(weekly_limit, daily_limit)
        except Exception as e:
            print(f"[usage] unexpected error: {e}", file=sys.stderr, flush=True)
            fresh = None

        if fresh is not None:
            with _lock:
                _usage_cache.update(fresh)
        else:
            print("[usage] no data – keeping cached values", flush=True)

        _post_to_device(url, headers, dry_run)
        _shutdown.wait(USAGE_INTERVAL)

# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall() -> None:
    """Remove all codelight hooks, socket file, and state directory."""

    # Broader check than install_hooks — also catches old claude_monitor references.
    def is_monitor_cmd(cmd: str) -> bool:
        return (("codelight" in cmd or "claude_monitor" in cmd) and "--hook" in cmd) \
               or "monitor_hook.py" in cmd

    settings_path = os.path.expanduser("~/.claude/settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        hooks = settings.get("hooks", {})
        changed = False
        for event in list(hooks.keys()):
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
                changed = True
                if cleaned:
                    hooks[event] = cleaned
                else:
                    del hooks[event]
        if changed:
            settings["hooks"] = hooks
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
                f.write("\n")
            print(f"[uninstall] removed hooks from {settings_path}")
        else:
            print("[uninstall] no codelight hooks found in settings.json")
    except FileNotFoundError:
        print("[uninstall] no settings.json found — nothing to remove")
    except Exception as e:
        print(f"[uninstall] could not update {settings_path}: {e}", file=sys.stderr)

    for path in [SOCKET_PATH, MONITOR_STATE_DIR]:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            print(f"[uninstall] removed {path}")
        except FileNotFoundError:
            pass

    print("[uninstall] done")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _verbose

    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove hooks from ~/.claude/settings.json and delete state files.")
    parser.add_argument("--hook", metavar="STATE",
                        help="Hook mode: send STATE event to daemon and exit. "
                             "Used internally by Claude Code hooks (working/waiting/ended).")
    parser.add_argument("--device", default="claude-screen.local",
                        help="Device hostname or IP (default: claude-screen.local)")
    parser.add_argument("--secret", default="",
                        help="Shared secret (X-Secret header)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Print payload instead of POSTing to device")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show socket events and usage API responses")
    parser.add_argument("--weekly-limit", type=int, default=0,
                        help="Max weekly requests for fallback progress bar (0=disable)")
    parser.add_argument("--daily-limit", type=int, default=0,
                        help="Max daily requests for fallback progress bar (0=disable)")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
        return

    if args.hook:
        run_hook(args.hook)
        return

    _verbose = args.verbose

    if not args.dry_run and _requests is None:
        sys.exit("Install requests:  pip install requests")

    install_hooks(os.path.abspath(__file__))

    url     = f"http://{args.device}/status"
    headers = {"Content-Type": "application/json"}
    if args.secret:
        headers["X-Secret"] = args.secret

    mode = "DRY RUN" if args.dry_run else f"posting to {url}"
    print(f"codelight  [{mode}]  (Ctrl-C to stop)", flush=True)

    threading.Thread(
        target=_socket_thread,
        args=(url, headers, args.dry_run),
        daemon=True,
    ).start()

    threading.Thread(
        target=_usage_thread,
        args=(url, headers, args.dry_run, args.weekly_limit, args.daily_limit),
        daemon=True,
    ).start()

    signal.signal(signal.SIGTERM, lambda *_: (_shutdown.set(), sys.exit(0)))

    try:
        while not _shutdown.is_set():
            _shutdown.wait(1.0)
    except KeyboardInterrupt:
        _shutdown.set()


if __name__ == "__main__":
    main()
