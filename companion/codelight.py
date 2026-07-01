#!/usr/bin/env python3
"""
codelight.py – pushes Claude Code status to codelight clients (screen + Android widget).

Usage:
    python3 codelight.py --name henrik-laptop
    python3 codelight.py --name henrik-laptop --dry-run   # print payload, no broadcast
    python3 codelight.py --dry-run --verbose              # also show socket events and API data
    python3 -u codelight.py | tee                         # -u avoids buffering when piping
"""
import argparse
import asyncio
import collections
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
    import websockets as _websockets
    _have_websockets = True
except ImportError:
    _have_websockets = False

try:
    from zeroconf import Zeroconf, ServiceInfo
    _have_zeroconf = True
except ImportError:
    _have_zeroconf = False

try:
    from dbus_fast.aio import MessageBus as _DbusMessageBus
    from dbus_fast.service import ServiceInterface as _DbusServiceInterface
    from dbus_fast.service import signal as _dbus_signal, method as _dbus_method
    from dbus_fast import BusType as _DbusBusType
    _have_dbus = True
except ImportError:
    _have_dbus = False

# ── Config ────────────────────────────────────────────────────────────────────

MONITOR_STATE_DIR = os.path.expanduser("~/.claude/monitor_state")
SOCKET_PATH       = os.path.expanduser("~/.claude/codelight.sock")
USAGE_INTERVAL      = 60   # seconds between usage API polls
IDLE_WINDOW         = 600  # seconds before a silent "working" session is dropped
IDLE_WINDOW_WAITING = 30   # seconds before a "waiting" session is dropped (subagents resolve quickly)

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

_ws_loop:    asyncio.AbstractEventLoop | None = None
_ws_clients: set = set()
_last_ws_status: str = "inactive"   # updated by _broadcast; watched by timeout-watchdog
_dbus_iface: object | None = None   # CodelightDbusInterface instance when D-Bus is available

_log_lines:       collections.deque = collections.deque(maxlen=10)
_last_payload:    dict | None = None
_render_lock:     threading.Lock = threading.Lock()
_dashboard_ready: bool = False   # True after the first full-screen clear

# ── Helpers ───────────────────────────────────────────────────────────────────

def vprint(*args, **kwargs):
    if _verbose:
        if sys.stdout.isatty():
            _log(" ".join(str(a) for a in args))
        else:
            print(*args, **kwargs, flush=True)


def _log(msg: str) -> None:
    """Append a timestamped line to the rolling activity log.
    In TTY mode the dashboard redraws immediately; in pipe mode it prints directly."""
    ts = datetime.now().strftime("%H:%M:%S")
    _log_lines.append(f"[{ts}] {msg}")
    if sys.stdout.isatty() and _last_payload is not None:
        with _render_lock:
            _render_dashboard(_last_payload)
    elif not sys.stdout.isatty():
        print(f"[{ts}] {msg}", flush=True)


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

def _get_local_ip() -> str:
    """Return the LAN IP this machine uses for outbound traffic."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _broadcast(payload: dict) -> None:
    """Thread-safe push to all WebSocket clients and the D-Bus signal."""
    global _last_ws_status
    _last_ws_status = payload.get("status", _last_ws_status)
    if _ws_loop is None:
        return
    msg = json.dumps(payload)

    async def _send_all() -> None:
        if _ws_clients:
            await asyncio.gather(
                *[c.send(msg) for c in list(_ws_clients)],
                return_exceptions=True,
            )
        if _dbus_iface is not None:
            try:
                _dbus_iface.StatusChanged(msg)  # type: ignore[union-attr]
            except Exception:
                pass

    asyncio.run_coroutine_threadsafe(_send_all(), _ws_loop)


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
                 if now - info["time"] > (IDLE_WINDOW_WAITING
                                          if info["state"] == "waiting"
                                          else IDLE_WINDOW)]
        for sid in stale:
            del _sessions[sid]
        for info in _sessions.values():
            active += 1
            if info["state"] == "working":
                overall = "working"
            elif info["state"] == "waiting" and overall != "working":
                overall = "waiting"
    return active, overall

# ── D-Bus interface ───────────────────────────────────────────────────────────

if _have_dbus:
    class CodelightDbusInterface(_DbusServiceInterface):  # type: ignore[misc]
        def __init__(self):
            super().__init__('se.sensnology.codelight')

        @_dbus_signal()
        def StatusChanged(self, status_json: str) -> 's':  # type: ignore[return]
            return status_json

        @_dbus_method()
        def GetStatus(self) -> 's':  # type: ignore[return]
            sessions, status = _overall_status()
            with _lock:
                usage = dict(_usage_cache)
            return json.dumps({**usage, 'sessions': sessions, 'status': status})

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
        "PreToolUse":       f"{cmd_base} working",
        "PostToolUse":      f"{cmd_base} working",
        "UserPromptSubmit": f"{cmd_base} working",
        "PermissionRequest": f"{cmd_base} waiting",
        "PermissionDenied":  f"{cmd_base} working",
        "Stop":              f"{cmd_base} ended",
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


def get_usage() -> dict | None:
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
        print(f"[usage] could not read credentials: {e}", file=sys.stderr, flush=True)
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
        print(f"[usage] HTTP {e.code}: {e.reason}", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"[usage] request error: {e}", file=sys.stderr, flush=True)
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

# ── Payload helpers ───────────────────────────────────────────────────────────

_STATUS_COLOR = {
    "working":  "\033[33m",   # orange
    "waiting":  "\033[31m",   # red
    "inactive": "\033[32m",   # green
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_BAR_W = 28


def _render_dashboard(payload: dict) -> None:
    """Write the top-like dashboard to stdout (caller holds _render_lock)."""
    global _dashboard_ready
    status  = payload["status"]
    color   = _STATUS_COLOR.get(status, "")
    ts      = datetime.now().strftime("%H:%M:%S")

    def bar(pct: float) -> str:
        filled = round(max(0.0, min(1.0, pct)) * _BAR_W)
        return "█" * filled + "░" * (_BAR_W - filled)

    ws_count = len(_ws_clients)
    parts: list[str] = []
    if ws_count:
        parts.append(f"{ws_count} WebSocket{'s' if ws_count != 1 else ''}")
    if _dbus_iface is not None:
        parts.append("D-Bus")
    clients_str = "  ".join(parts) if parts else "none"

    sessions = payload["sessions"]
    lines = [
        f"{_BOLD}CODELIGHT{_RESET}",
        f"  Updated:  {ts}",
        f"  Clients:  {clients_str}",
        "",
        f"  {color}● {status.upper()}{_RESET}  "
        f"{_DIM}({sessions} session{'s' if sessions != 1 else ''}){_RESET}",
        "",
        f"  Weekly   {bar(payload['weekly_pct'])} {payload['weekly_pct']:>4.0%}"
        f"  {_DIM}resets {payload['weekly_reset']}{_RESET}",
        f"  Session  {bar(payload['session_pct'])} {payload['session_pct']:>4.0%}"
        f"  {_DIM}resets {payload['session_reset']}{_RESET}",
        "",
        f"  {_DIM}Recent activity{_RESET}",
    ] + [f"  {ln}" for ln in _log_lines]

    # First render: clear the whole screen (removes startup messages).
    # Subsequent renders: move to top-left and overwrite in-place.
    # Append \033[K (erase to EOL) to every line so leftover characters
    # from a previous longer line don't bleed through on the right.
    prefix = "\033[2J\033[H" if not _dashboard_ready else "\033[H"
    _dashboard_ready = True
    cleared = [ln + "\033[K" for ln in lines]
    sys.stdout.write(prefix + "\n".join(cleared) + "\033[J")
    sys.stdout.flush()


def print_payload(payload: dict) -> None:
    """Update the live dashboard (TTY). In non-TTY mode just tracks state for _log()."""
    global _last_payload
    _last_payload = payload
    if sys.stdout.isatty():
        with _render_lock:
            _render_dashboard(payload)


def _push() -> None:
    """Build payload from current state and broadcast to all clients."""
    sessions, status = _overall_status()
    with _lock:
        usage = dict(_usage_cache)
    payload = {**usage, "sessions": sessions, "status": status}
    _broadcast(payload)
    print_payload(payload)

# ── Daemon threads ────────────────────────────────────────────────────────────

def _ws_thread(port: int, secret: str) -> None:
    """Run a WebSocket server; screen and Android clients connect here for live updates."""
    global _ws_loop, _ws_clients

    if not _have_websockets:
        print("[ws] websockets not installed — clients unavailable", file=sys.stderr)
        print("[ws] Install: pip install websockets", file=sys.stderr)
        return

    async def handler(ws, *_) -> None:
        if secret:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                if json.loads(msg).get("auth") != secret:
                    _log(f"[ws] auth failed from {ws.remote_address}")
                    try:
                        await ws.send(json.dumps({"error": "unauthorized", "message": "Wrong password"}))
                    except Exception:
                        pass
                    await ws.close(1008, "Unauthorized")
                    return
            except Exception:
                _log(f"[ws] auth error from {ws.remote_address}")
                try:
                    await ws.send(json.dumps({"error": "unauthorized", "message": "Wrong password"}))
                except Exception:
                    pass
                await ws.close(1008, "Unauthorized")
                return

        _ws_clients.add(ws)
        _log(f"[ws] client connected ({len(_ws_clients)} total)")
        try:
            # Push timezone offset so the screen can configure NTP correctly
            utc_offset = int(datetime.now().astimezone().utcoffset().total_seconds())
            await ws.send(json.dumps({"type": "config", "utc_offset": utc_offset}))

            # Send current state immediately so the client isn't blank on connect
            sessions, status = _overall_status()
            with _lock:
                usage = dict(_usage_cache)
            await ws.send(json.dumps({**usage, "sessions": sessions, "status": status}))
            try:
                await ws.wait_closed()
            except Exception:
                pass  # connection reset without close frame — normal on app restart
        finally:
            _ws_clients.discard(ws)
            _log(f"[ws] client disconnected ({len(_ws_clients)} remaining)")

    async def serve() -> None:
        global _last_ws_status, _dbus_iface

        if _have_dbus:
            try:
                dbus_bus = await _DbusMessageBus(bus_type=_DbusBusType.SESSION).connect()
                iface = CodelightDbusInterface()  # type: ignore[name-defined]
                dbus_bus.export('/se/sensnology/codelight', iface)
                await dbus_bus.request_name('se.sensnology.codelight')
                _dbus_iface = iface
                _log("[dbus] service exported")
            except Exception as e:
                print(f"[dbus] setup failed: {e}", file=sys.stderr, flush=True)

        async with _websockets.serve(handler, "0.0.0.0", port):
            vprint(f"[ws] listening on :{port}")
            while not _shutdown.is_set():
                await asyncio.sleep(2)
                if not _ws_clients and _dbus_iface is None:
                    continue
                # Detect status changes caused by session timeouts (no hook fires for those).
                sessions, current_status = _overall_status()
                if current_status != _last_ws_status:
                    _last_ws_status = current_status
                    with _lock:
                        usage = dict(_usage_cache)
                    payload = {**usage, "sessions": sessions, "status": current_status}
                    msg = json.dumps(payload)
                    print_payload(payload)  # update dashboard status immediately
                    _log(f"[ws] timeout → {current_status}")
                    if _ws_clients:
                        await asyncio.gather(
                            *[c.send(msg) for c in list(_ws_clients)],
                            return_exceptions=True,
                        )
                    if _dbus_iface is not None:
                        try:
                            _dbus_iface.StatusChanged(msg)  # type: ignore[union-attr]
                        except Exception:
                            pass

    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)
    try:
        _ws_loop.run_until_complete(serve())
    except Exception as e:
        print(f"[ws] server error: {e}", file=sys.stderr)
    finally:
        _ws_loop.close()
        _ws_loop = None


def _mdns_thread(port: int, name: str) -> None:
    """Advertise the WebSocket service via mDNS so clients find it automatically.
    Re-registers whenever the local IP changes (e.g. switching WiFi networks)."""
    if not _have_zeroconf:
        print("[mdns] zeroconf not installed — skipping advertisement", file=sys.stderr)
        print("[mdns] Install: pip install zeroconf", file=sys.stderr)
        return

    zc: Zeroconf | None = None
    current_ip: str | None = None
    info = None
    while not _shutdown.is_set():
        ip = _get_local_ip()

        # Skip loopback — no network yet (e.g. just woke from sleep).
        # Retry quickly so we pick up the real IP as soon as it's available.
        if ip.startswith("127."):
            if current_ip is not None:
                # Network just went away — tear down so we re-register when it returns
                if info is not None and zc is not None:
                    try:
                        zc.unregister_service(info)
                    except Exception:
                        pass
                if zc is not None:
                    try:
                        zc.close()
                    except Exception:
                        pass
                zc = None
                info = None
                current_ip = None
                _log("[mdns] network lost, waiting for reconnect…")
            _shutdown.wait(5)
            continue

        if ip != current_ip:
            # Tear down old instance before rebinding to the new interface
            if info is not None and zc is not None:
                try:
                    zc.unregister_service(info)
                except Exception:
                    pass
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass
            zc = None
            info = None
            try:
                # Bind to the specific IPv4 interface so the mDNS response stays
                # small — the ESP8266 UDP buffer drops oversized multi-interface packets
                zc = Zeroconf(interfaces=[ip])
                info = ServiceInfo(
                    "_codelight._tcp.local.",
                    f"{name}._codelight._tcp.local.",
                    addresses=[socket.inet_aton(ip)],
                    port=port,
                    properties={},
                )
                zc.register_service(info)
                current_ip = ip
                _log(f"[mdns] advertising on {ip}:{port}")
            except Exception as e:
                _log(f"[mdns] registration failed: {e}")
                if zc is not None:
                    try:
                        zc.close()
                    except Exception:
                        pass
                zc = None
                info = None
                # Don't update current_ip — forces a retry next iteration
                _shutdown.wait(5)
                continue

        _shutdown.wait(10)   # re-check IP every 10 s

    if info is not None and zc is not None:
        try:
            zc.unregister_service(info)
        except Exception:
            pass
    if zc is not None:
        zc.close()
    vprint("[mdns] stopped")


def _socket_thread() -> None:
    """Accept hook events on the Unix socket and broadcast to clients immediately."""
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
                raw      = conn.recv(4096).decode()
                msg      = json.loads(raw)
                sid   = msg.get("session_id", "unknown")
                state = msg.get("state", "")

                if state:
                    _update_session(sid, state)
                    vprint(f"[socket] {sid[:8]}… → {state}")
                    _push()
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


def _usage_thread() -> None:
    """Poll the usage API every USAGE_INTERVAL seconds and broadcast after each update."""
    while not _shutdown.is_set():
        _log("[usage] polling…")
        try:
            fresh = get_usage()
        except Exception as e:
            print(f"[usage] unexpected error: {e}", file=sys.stderr, flush=True)
            fresh = None

        if fresh is not None:
            with _lock:
                _usage_cache.update(fresh)
            _log(f"[usage] session={fresh['session_pct']:.0%}  weekly={fresh['weekly_pct']:.0%}")
        else:
            _log("[usage] no data – keeping cached values")

        _push()
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

    service_path = os.path.expanduser("~/.config/systemd/user/codelight.service")
    if os.path.exists(service_path):
        import subprocess
        subprocess.run(["systemctl", "--user", "disable", "--now", "codelight"],
                       capture_output=True)
        os.unlink(service_path)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print(f"[uninstall] removed {service_path}")

    print("[uninstall] done")


# ── Systemd service install ───────────────────────────────────────────────────

def install_service(name: str, secret: str, ws_port: int, verbose: bool) -> None:
    """Write ~/.config/systemd/user/codelight.service and enable it."""
    import subprocess

    script_path = os.path.abspath(__file__)
    python_path = shutil.which("python3") or "python3"

    args_line = f"--name {name}"
    if secret:
        args_line += f" --secret {secret}"
    if ws_port != 8765:
        args_line += f" --ws-port {ws_port}"
    if verbose:
        args_line += " --verbose"

    unit = f"""\
[Unit]
Description=Claude Code status monitor

[Service]
ExecStart={python_path} -u {script_path} {args_line}
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
"""

    service_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(service_dir, exist_ok=True)
    service_path = os.path.join(service_dir, "codelight.service")

    with open(service_path, "w") as f:
        f.write(unit)
    print(f"[install] wrote {service_path}")

    for cmd in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "codelight"],
    ]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        label = " ".join(cmd[2:])
        if result.returncode == 0:
            print(f"[install] systemctl {label}: ok")
        else:
            print(f"[install] systemctl {label}: {result.stderr.strip()}", file=sys.stderr)

    print("[install] done — check status with: systemctl --user status codelight")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _verbose

    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove hooks from ~/.claude/settings.json and delete state files.")
    parser.add_argument("--install", action="store_true",
                        help="Install and start a systemd user service (requires --name).")
    parser.add_argument("--hook", metavar="STATE",
                        help="Hook mode: send STATE event to daemon and exit. "
                             "Used internally by Claude Code hooks (working/waiting/ended).")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show low-level debug events (socket, API) in activity log")
    parser.add_argument("--ws-port", type=int, default=8765,
                        help="WebSocket port for clients (default: 8765)")
    parser.add_argument("--name", default=None,
                        help="mDNS service name visible to clients (required)")
    parser.add_argument("--secret", default="",
                        help="Shared secret for WebSocket auth (match in screen config)")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
        return

    if args.install:
        if args.name is None:
            parser.error("--name is required with --install")
        install_service(args.name, args.secret, args.ws_port, args.verbose)
        return

    if args.hook:
        run_hook(args.hook)
        return

    if args.name is None:
        parser.error("--name is required (e.g. --name henrik-laptop). "
                     "It identifies this daemon to clients.")

    _verbose = args.verbose

    install_hooks(os.path.abspath(__file__))

    print(f"codelight  [ws://0.0.0.0:{args.ws_port}]  (Ctrl-C to stop)", flush=True)

    threading.Thread(target=_socket_thread, daemon=True).start()
    threading.Thread(target=_usage_thread,  daemon=True).start()

    threading.Thread(
        target=_ws_thread,
        args=(args.ws_port, args.secret),
        daemon=True,
    ).start()

    threading.Thread(
        target=_mdns_thread,
        args=(args.ws_port, args.name),
        daemon=True,
    ).start()

    print(f"daemon ready — next usage poll in {USAGE_INTERVAL}s", flush=True)

    signal.signal(signal.SIGTERM, lambda *_: (_shutdown.set(), sys.exit(0)))

    try:
        while not _shutdown.is_set():
            _shutdown.wait(1.0)
    except KeyboardInterrupt:
        _shutdown.set()


if __name__ == "__main__":
    main()
