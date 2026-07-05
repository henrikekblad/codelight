#!/usr/bin/env python3
"""
codelight.py – pushes Claude Code status to codelight clients (screen + Android widget).

Usage:
    python3 codelight.py --name henrik-laptop
    python3 codelight.py --name henrik-laptop --verbose   # also show socket events and API data
    python3 -u codelight.py | tee                         # -u avoids buffering when piping
"""
import argparse
import asyncio
import collections
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import sys
import threading
import time
import uuid
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
# Hard ceiling a remote-control hook will block, in case the daemon dies. The
# daemon normally replies far sooner (at its idle timeout, or on answer); a
# client keepalive can extend up to this. Claude Code's own hook timeout is set
# just above it.
HOOK_WAIT_CEILING = 590

# ── Module-level state ────────────────────────────────────────────────────────

_verbose  = False
_shutdown = threading.Event()

_lock: threading.Lock = threading.Lock()
# session_id → {"state": "working"|"waiting", "time": float}
_sessions: dict[str, dict] = {}
_usage_cache: dict = {
    "session_pct": 0.0, "weekly_pct": 0.0,
    "session_reset": "--", "weekly_reset": "--",
    "session_reset_at": 0, "weekly_reset_at": 0,
}

_ws_loop:    asyncio.AbstractEventLoop | None = None
_ws_clients: set = set()
_last_ws_status: str = "idle"   # updated by _broadcast; watched by timeout-watchdog
_dbus_iface: object | None = None   # CodelightDbusInterface instance when D-Bus is available

# Remote control (armed via --remote-control / --remote-permissions, requires --secret):
# approve tool permissions AND answer AskUserQuestion prompts remotely.
_remote_permissions: bool = False
_remote_questions:   bool = False
_permission_timeout: int  = 60
# request_id → {"conn", "id", "session_id", "tool_name", "summary", "tool_input",
#               "cwd", "event", "decision", "by", "expires"}
_pending_perms: dict[str, dict] = {}
# request_id → {"conn", "id", "session_id", "tool_input", "questions",
#               "event", "answers", "by", "expires"}
_pending_questions: dict[str, dict] = {}
_perm_clients: set = set()   # WS clients subscribed to remote-control events
_conv_clients: set = set()   # WS clients subscribed to the conversation feed
_question_clients: set = set()  # WS clients that will answer AskUserQuestion prompts
# GNOME answers over D-Bus (not a WS subscriber), so it announces its presence:
# question fall-through must not fire while a GNOME extension is listening.
GNOME_PRESENCE_TTL = 90
_gnome_last_seen: float = 0.0
_gnome_features: set = set()
# When a question-answering client was last connected, so a client that is
# merely reconnecting (e.g. VSCode restarting) isn't mistaken for "nobody home"
# and cut off before it re-subscribes.
_last_qclient_gone: float = 0.0
# Last transcript we saw, kept even after the session ends so the trailing
# assistant message (flushed just after the Stop hook) still reaches clients.
_last_transcript: dict = {"sid": "", "path": ""}
_last_conv_mtime: float = 0.0

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


def _epoch(iso_ts: str) -> int:
    """ISO-8601 timestamp → epoch seconds (0 if unparseable)."""
    if not iso_ts:
        return 0
    try:
        return int(datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


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

def _update_session(session_id: str, state: str,
                    transcript: str = "", cwd: str = "") -> None:
    global _last_transcript
    if transcript:
        _last_transcript = {"sid": session_id, "path": transcript}
    with _lock:
        if state == "ended":
            _sessions.pop(session_id, None)
        else:
            info = _sessions.get(session_id, {})
            info["state"] = state
            info["time"]  = time.time()
            # Keep the last-known transcript/cwd when a later event omits them
            # (e.g. PermissionRequest carries cwd but no transcript_path).
            if transcript:
                info["transcript"] = transcript
            if cwd:
                info["cwd"] = cwd
            _sessions[session_id] = info


def _active_transcript() -> tuple[str, str]:
    """(session_id, transcript_path) of the most-recently-active session that
    has a known transcript. Falls back to the last transcript we ever saw so
    the trailing message survives the session being popped on Stop."""
    with _lock:
        best = None
        for sid, info in _sessions.items():
            if info.get("transcript"):
                if best is None or info["time"] > best[1]:
                    best = (sid, info["time"], info["transcript"])
    if best:
        return (best[0], best[2])
    if _last_transcript["path"]:
        return (_last_transcript["sid"], _last_transcript["path"])
    return ("", "")


def _parse_transcript(path: str, max_msgs: int = 60) -> list[dict]:
    """Best-effort parse of a Claude Code transcript JSONL into a list of
    {"role", "text"} dicts (newest last). The transcript format is INTERNAL to
    Claude Code and may change without notice, so this must never raise."""
    try:
        with open(path, "r") as f:
            raw_lines = f.readlines()
    except Exception:
        return []

    out: list[dict] = []
    # Only scan the tail; each turn can span several lines (tool_use/result).
    for raw in raw_lines[-8 * max_msgs:]:
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if o.get("isMeta") or o.get("isCompactSummary"):
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = o.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or t
        content = msg.get("content")

        if isinstance(content, str):
            s = content.strip()
            if s and not _is_noise(s):
                out.append({"role": role, "text": s[:2000]})
        elif isinstance(content, list):
            # Emit the human/assistant prose first, then each tool call and its
            # output as their own lines so the phone shows what actually happened.
            prose: list[str] = []
            tail: list[dict] = []
            for block in content:
                if isinstance(block, str):
                    prose.append(block); continue
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    prose.append(block.get("text", ""))
                elif bt == "image":
                    prose.append("[image]")
                elif bt == "tool_use":
                    tail.append({"role": "tool",
                                 "text": _tool_summary(block.get("name", "?"),
                                                       block.get("input") or {})})
                elif bt == "tool_result":
                    snippet = _tool_result_text(block.get("content"))
                    if snippet:
                        tail.append({"role": "output", "text": "⤷ " + snippet[:400]})
                # thinking → skip
            prose_text = "\n".join(p for p in prose if p).strip()
            if prose_text and not _is_noise(prose_text):
                out.append({"role": role, "text": prose_text[:2000]})
            out.extend(tail)

    return out[-max_msgs:]


def _is_noise(s: str) -> bool:
    """True for machine-generated wrappers (slash-commands, IDE hints, injected
    reminders) that aren't turns the human actually typed."""
    return ("<command-" in s or "<system-reminder" in s or "<ide_" in s
            or "<local-command" in s or s.startswith("Caveat:"))


def _tool_result_text(content) -> str:
    """Extract a short plain-text snippet from a tool_result block's content."""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        s = "\n".join(parts)
    else:
        s = ""
    return " ".join(s.split())   # collapse whitespace to one line


def _conversation_payload() -> dict | None:
    """Build the {"type":"conversation", ...} feed for the active session."""
    sid, path = _active_transcript()
    if not path:
        return None
    return {"type": "conversation", "session_id": sid,
            "lines": _parse_transcript(path)}


def _conv_poll_thread() -> None:
    """Push the conversation feed whenever the active transcript grows. This is
    the safety net for the last assistant message: the Stop hook fires (and pops
    the session) before Claude Code flushes its final line to the JSONL, so a
    hook-only feed would miss it until the next user prompt."""
    global _last_conv_mtime
    while not _shutdown.is_set():
        time.sleep(1.0)
        if not _conv_clients:
            continue
        _, path = _active_transcript()
        if not path:
            continue
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if m != _last_conv_mtime:
            _last_conv_mtime = m
            _broadcast_conversation()


def _broadcast_conversation() -> None:
    """Push the conversation feed to subscribed clients (thread-safe)."""
    if _ws_loop is None or not _conv_clients:
        return
    payload = _conversation_payload()
    if payload is None:
        return
    msg = json.dumps(payload)

    async def _send() -> None:
        targets = [c for c in list(_conv_clients) if c in _ws_clients]
        if targets:
            await asyncio.gather(*[c.send(msg) for c in targets],
                                 return_exceptions=True)

    asyncio.run_coroutine_threadsafe(_send(), _ws_loop)


def _overall_status() -> tuple[int, str]:
    """Return (active_count, overall_status) from in-memory session state.
    Cleans up sessions that have been silent longer than IDLE_WINDOW."""
    now = time.time()
    active  = 0
    overall = "idle"
    with _lock:
        # Sessions with a pending remote permission/question request stay alive —
        # the 30 s waiting window would otherwise drop them mid-request
        pending_sids = ({p["session_id"] for p in _pending_perms.values()}
                        | {q["session_id"] for q in _pending_questions.values()})
        stale = [sid for sid, info in _sessions.items()
                 if sid not in pending_sids
                 and now - info["time"] > (IDLE_WINDOW_WAITING
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

        @_dbus_signal()
        def PermissionRequest(self, request_json: str) -> 's':  # type: ignore[return]
            return request_json

        @_dbus_signal()
        def PermissionResolved(self, resolved_json: str) -> 's':  # type: ignore[return]
            return resolved_json

        @_dbus_method()
        def RespondPermission(self, request_id: 's', decision: 's') -> 'b':  # type: ignore[return]
            # Session bus = same local user → inside the trust boundary
            return _resolve_permission(request_id, decision, 'gnome')

        @_dbus_signal()
        def QuestionRequest(self, request_json: str) -> 's':  # type: ignore[return]
            return request_json

        @_dbus_signal()
        def QuestionResolved(self, resolved_json: str) -> 's':  # type: ignore[return]
            return resolved_json

        @_dbus_method()
        def RespondQuestion(self, request_id: 's', answers_json: 's') -> 'b':  # type: ignore[return]
            try:
                answers = json.loads(answers_json)
            except Exception:
                return False
            return _resolve_question(request_id, answers, 'gnome')

        @_dbus_method()
        def ExtendRequest(self, request_id: 's') -> 'b':  # type: ignore[return]
            # Keepalive while the GNOME prompt is open, so it doesn't time out
            return _extend_request(request_id)

        @_dbus_method()
        def Announce(self, features_json: 's') -> 'b':  # type: ignore[return]
            # The GNOME extension announces (on enable + a periodic heartbeat)
            # which features it can answer, so question fall-through doesn't fire
            # while it's listening. Not a WS subscriber, so it can't be counted
            # any other way.
            global _gnome_last_seen, _gnome_features
            try:
                feats = json.loads(features_json)
            except Exception:
                feats = []
            _gnome_last_seen = time.time()
            _gnome_features = set(feats) if isinstance(feats, list) else set()
            return True

# ── Remote permission approval ────────────────────────────────────────────────
#
# Flow: the PermissionRequest hook (--hook permission) blocks on the Unix
# socket; the daemon forwards the request to subscribed WS clients + D-Bus,
# and whoever answers first (VSCode / Android / GNOME) decides. On timeout the
# hook prints nothing and Claude Code falls back to its built-in prompt.
# Permission messages are only sent to clients that subscribed — old clients
# (ESP32 screen, older apps) never see them.

def _broadcast_rc(payload: dict, dbus_signal: str) -> None:
    """Send a remote-control event (permission/question) to subscribed WS
    clients and emit the named D-Bus signal."""
    if _ws_loop is None:
        return
    msg = json.dumps(payload)

    async def _send() -> None:
        if _perm_clients:
            await asyncio.gather(
                *[c.send(msg) for c in list(_perm_clients)],
                return_exceptions=True,
            )
        if _dbus_iface is not None:
            try:
                getattr(_dbus_iface, dbus_signal)(msg)   # type: ignore[union-attr]
            except Exception:
                pass

    asyncio.run_coroutine_threadsafe(_send(), _ws_loop)


def _perm_request_payload(entry: dict) -> dict:
    return {
        "type":       "permission_request",
        "id":         entry["id"],
        "tool_name":  entry["tool_name"],
        "summary":    entry["summary"],
        "tool_input": entry["tool_input"],
        "session_id": entry["session_id"],
        "cwd":        entry["cwd"],
        "expires_at": int(entry["expires"]),
    }


def _resolve_permission(request_id: str, decision: str, by: str) -> bool:
    """Record a decision for a pending request. First response wins."""
    if decision not in ("allow", "deny"):
        return False
    with _lock:
        entry = _pending_perms.get(request_id)
        if entry is None or entry["decision"] is not None or entry["by"] is not None:
            return False
        entry["decision"] = decision
        entry["by"] = by
    entry["event"].set()
    return True


def _wait_with_extend(entry: dict) -> None:
    """Block until the request is resolved (event set) or its deadline passes.
    Re-reads entry['expires'] each loop so a client keepalive (_extend_request)
    can push the deadline out while a human is still interacting."""
    while not entry["event"].is_set():
        remaining = entry["expires"] - time.time()
        if remaining <= 0:
            break
        entry["event"].wait(min(remaining, 5.0))


# Grace window for a question to reach an answering client before it falls
# through to Claude's local dialog. Long enough to survive a client
# reconnecting (e.g. VSCode restart) — the daemon replays pending requests on
# subscribe — short enough that a truly unattended session isn't stuck waiting.
NO_CLIENT_GRACE = 6


RECONNECT_WINDOW = 30


def _wait_question(entry: dict) -> None:
    """Like _wait_with_extend, but if no client can answer questions the request
    only lives for NO_CLIENT_GRACE seconds before falling through. A client that
    was connected within RECONNECT_WINDOW seconds is treated as merely
    reconnecting (e.g. VSCode restarting) — the normal extendable deadline
    applies so the replayed prompt reaches it. Only a session that has had no
    answering client present or recently gone falls through quickly."""
    grace_deadline = time.time() + NO_CLIENT_GRACE
    while not entry["event"].is_set():
        now = time.time()
        if _can_answer_questions() or (now - _last_qclient_gone) < RECONNECT_WINDOW:
            remaining = entry["expires"] - now
        else:
            remaining = grace_deadline - now
        if remaining <= 0:
            break
        entry["event"].wait(min(remaining, 2.0))


def _extend_request(request_id: str) -> bool:
    """Client keepalive: reset a pending request's idle deadline (called while
    a remote client has the prompt open, so it never times out mid-interaction)."""
    with _lock:
        e = _pending_perms.get(request_id) or _pending_questions.get(request_id)
        if e is None:
            return False
        e["expires"] = time.time() + _permission_timeout
    return True


def _cancel_permissions_for(session_id: str) -> None:
    """Session activity/end — wake up its pending permission AND question
    requests without a decision (answered locally)."""
    with _lock:
        perms = [e for e in _pending_perms.values()
                 if e["session_id"] == session_id and e["decision"] is None]
        for e in perms:
            e["by"] = "cancelled"
        ques = [e for e in _pending_questions.values()
                if e["session_id"] == session_id and e["by"] is None]
        for e in ques:
            e["by"] = "cancelled"
    for e in perms + ques:
        e["event"].set()


def _permission_waiter(entry: dict) -> None:
    """Per-request thread: wait for a decision (or timeout), reply to the
    blocked hook on its held connection, and notify clients."""
    _wait_with_extend(entry)
    with _lock:
        _pending_perms.pop(entry["id"], None)
        decision = entry["decision"]
        by       = entry["by"]

    try:
        entry["conn"].sendall((json.dumps({"decision": decision}) + "\n").encode())
    except Exception:
        pass
    try:
        entry["conn"].close()
    except Exception:
        pass

    outcome = decision or ("cancelled" if by == "cancelled" else "timeout")
    _log(f"[perm] {entry['summary'][:60]} → {outcome}"
         + (f" (by {by})" if decision else ""))
    _broadcast_rc({
        "type": "permission_resolved",
        "id": entry["id"],
        "decision": outcome,
        "by": by or "",
    }, "PermissionResolved")
    _push()


def _register_permission(conn, msg: dict) -> None:
    """Take ownership of the hook's socket connection and start the approval
    round-trip. Called from the socket thread; must not block it."""
    if not _remote_permissions:
        # Feature off (e.g. stale hook entry) — release the hook immediately
        try:
            conn.sendall(b'{"decision": null}\n')
        except Exception:
            pass
        conn.close()
        return

    rid = str(msg.get("prompt_id") or "") or uuid.uuid4().hex
    sid = msg.get("session_id", "unknown")
    entry = {
        "conn":       conn,
        "id":         rid,
        "session_id": sid,
        "tool_name":  msg.get("tool_name", "?"),
        "summary":    msg.get("summary", "") or msg.get("tool_name", "?"),
        "tool_input": msg.get("tool_input", {}),
        "cwd":        msg.get("cwd", ""),
        "event":      threading.Event(),
        "decision":   None,
        "by":         None,
        "expires":    time.time() + _permission_timeout,
    }
    with _lock:
        _pending_perms[rid] = entry
    _update_session(sid, "waiting")
    _log(f"[perm] request: {entry['summary'][:60]}")
    _push()
    _broadcast_rc(_perm_request_payload(entry), "PermissionRequest")
    threading.Thread(target=_permission_waiter, args=(entry,), daemon=True).start()


# ── Remote question answering (AskUserQuestion via PreToolUse) ─────────────────

def _question_request_payload(entry: dict) -> dict:
    return {
        "type":       "question_request",
        "id":         entry["id"],
        "questions":  entry["questions"],
        "session_id": entry["session_id"],
        "cwd":        entry["cwd"],
        "expires_at": int(entry["expires"]),
    }


def _resolve_question(request_id: str, answers, by: str) -> bool:
    """Resolve a pending question. First response wins. A non-empty dict of
    {question: answer_string} answers it; an empty/None answers is an explicit
    skip (reply null → hook falls through to Claude's dialog immediately)."""
    with _lock:
        entry = _pending_questions.get(request_id)
        if entry is None or entry["by"] is not None:   # already resolved
            return False
        entry["by"] = by
        if isinstance(answers, dict) and answers:
            entry["answers"] = answers   # else leave None → skip/fall-through
    entry["event"].set()
    return True


def _question_waiter(entry: dict) -> None:
    """Per-request thread: wait for answers (or timeout), reply to the blocked
    hook, and notify clients. Reply {"answers": {...}} → hook emits updatedInput;
    {"answers": null} → hook prints nothing → Claude's local dialog."""
    _wait_question(entry)
    with _lock:
        _pending_questions.pop(entry["id"], None)
        answers = entry["answers"]
        by      = entry["by"]

    try:
        entry["conn"].sendall((json.dumps({"answers": answers}) + "\n").encode())
    except Exception:
        pass
    try:
        entry["conn"].close()
    except Exception:
        pass

    outcome = "answered" if answers else ("cancelled" if by == "cancelled" else "timeout")
    _log(f"[question] {entry['id'][:8]}… → {outcome}" + (f" (by {by})" if answers else ""))
    _broadcast_rc({
        "type": "question_resolved",
        "id": entry["id"],
        "by": by or "",
    }, "QuestionResolved")
    _push()


def _gnome_present(feature: str) -> bool:
    """True if a GNOME extension announced it can answer `feature` recently."""
    return (time.time() - _gnome_last_seen < GNOME_PRESENCE_TTL
            and feature in _gnome_features)


def _can_answer_questions() -> bool:
    """True if any client (WS or GNOME) is currently able to answer questions."""
    return bool(_question_clients) or _gnome_present("questions")


def _note_qclient_gone() -> None:
    """Record that a question-answering WS client just disconnected, so a brief
    reconnect (VSCode restart) isn't mistaken for an unattended session."""
    global _last_qclient_gone
    _last_qclient_gone = time.time()


def _register_question(conn, msg: dict) -> None:
    """Take ownership of the hook's socket connection and start the answer
    round-trip for an AskUserQuestion PreToolUse hook."""
    if not _remote_questions:
        try:
            conn.sendall(b'{"answers": null}\n')
        except Exception:
            pass
        conn.close()
        return

    rid = str(msg.get("prompt_id") or "") or uuid.uuid4().hex
    sid = msg.get("session_id", "unknown")
    entry = {
        "conn":       conn,
        "id":         rid,
        "session_id": sid,
        "questions":  msg.get("questions", []),
        "cwd":        msg.get("cwd", ""),
        "event":      threading.Event(),
        "answers":    None,
        "by":         None,
        "expires":    time.time() + _permission_timeout,
    }
    with _lock:
        _pending_questions[rid] = entry
    _update_session(sid, "waiting")
    _log(f"[question] request: {len(entry['questions'])} question(s)")
    _push()
    _broadcast_rc(_question_request_payload(entry), "QuestionRequest")
    threading.Thread(target=_question_waiter, args=(entry,), daemon=True).start()


# ── Hook installation ─────────────────────────────────────────────────────────

def install_hooks(script_path: str, remote_permissions: bool = False,
                  remote_questions: bool = False, permission_timeout: int = 60) -> None:
    """
    Ensure ~/.claude/settings.json has the monitor hooks pointing to this script.
    Idempotent: safe to call on every startup. Preserves all non-monitor hooks.
    With remote_permissions the PermissionRequest hook blocks for a remote
    decision; with remote_questions a PreToolUse hook (matcher AskUserQuestion)
    blocks for a remote answer.
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
    if remote_permissions:
        # Hook-side wait is permission_timeout; give Claude Code's own hook
        # timeout headroom above that so it never kills a deciding hook.
        perm_hook = {"type": "command",
                     "command": f"{cmd_base} permission --permission-timeout {permission_timeout}",
                     "timeout": HOOK_WAIT_CEILING + 15}
    else:
        perm_hook = {"type": "command", "command": f"{cmd_base} waiting"}

    # desired: list of (event, matcher, hook_dict) — an event may appear more
    # than once with different matchers (e.g. PreToolUse status + question).
    desired = [
        ("PreToolUse",        "", {"type": "command", "command": f"{cmd_base} working"}),
        ("PostToolUse",       "", {"type": "command", "command": f"{cmd_base} working"}),
        ("UserPromptSubmit",  "", {"type": "command", "command": f"{cmd_base} working"}),
        ("PermissionRequest", "", perm_hook),
        ("PermissionDenied",  "", {"type": "command", "command": f"{cmd_base} working"}),
        ("Stop",              "", {"type": "command", "command": f"{cmd_base} ended"}),
        ("SessionEnd",        "", {"type": "command", "command": f"{cmd_base} ended"}),
    ]
    if remote_questions:
        desired.append(("PreToolUse", "AskUserQuestion", {
            "type": "command",
            "command": f"{cmd_base} question --permission-timeout {permission_timeout}",
            "timeout": HOOK_WAIT_CEILING + 15}))

    def is_monitor_cmd(cmd: str) -> bool:
        return ("codelight" in cmd and "--hook" in cmd) \
               or "monitor_hook.py" in cmd

    hooks = settings.get("hooks", {})
    before = json.dumps(hooks, sort_keys=True)

    # 1. Strip every codelight/monitor command from all events, preserving any
    #    unrelated hooks (and non-empty entries).
    for event in list(hooks.keys()):
        cleaned = []
        for entry in hooks.get(event, []):
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue
            inner = [c for c in entry.get("hooks", [])
                     if not (isinstance(c, dict) and is_monitor_cmd(c.get("command", "")))]
            if inner:
                cleaned.append({**entry, "hooks": inner})
            elif not entry.get("hooks"):
                cleaned.append(entry)   # entry that never had a hooks list
        if cleaned:
            hooks[event] = cleaned
        else:
            del hooks[event]

    # 2. Add the desired entries, merging into an existing matching-matcher entry.
    for event, matcher, hook_dict in desired:
        entries = hooks.setdefault(event, [])
        slot = next((e for e in entries
                     if isinstance(e, dict) and e.get("matcher", "") == matcher), None)
        if slot is None:
            entries.append({"matcher": matcher, "hooks": [hook_dict]})
        else:
            slot.setdefault("hooks", []).append(hook_dict)

    changed = json.dumps(hooks, sort_keys=True) != before

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
        sock.sendall(json.dumps({
            "state": state,
            "session_id": session_id,
            # Let the daemon tail this session's conversation for the app feed.
            "transcript_path": data.get("transcript_path", ""),
            "cwd": data.get("cwd", ""),
        }).encode())
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


def _truncate_tool_input(tool_input, max_str: int = 500, max_total: int = 3000):
    """Bound tool_input for transport: long strings clipped, payload capped."""
    def clip(v, depth=0):
        if isinstance(v, str):
            return v if len(v) <= max_str else v[:max_str] + "…"
        if isinstance(v, dict) and depth < 4:
            return {k: clip(x, depth + 1) for k, x in list(v.items())[:20]}
        if isinstance(v, list) and depth < 4:
            return [clip(x, depth + 1) for x in v[:10]]
        return v

    out = clip(tool_input)
    try:
        if len(json.dumps(out)) > max_total:
            return {"_truncated": json.dumps(out)[:max_total] + "…"}
    except Exception:
        return {}
    return out


def _tool_summary(tool_name: str, tool_input: dict) -> str:
    """One-line human summary of what Claude wants to do."""
    if tool_name == "Bash":
        detail = tool_input.get("command", "")
    elif tool_name in ("Edit", "Write", "Read", "NotebookEdit"):
        detail = tool_input.get("file_path", "")
    elif tool_name in ("WebFetch", "WebSearch"):
        detail = tool_input.get("url", "") or tool_input.get("query", "")
    else:
        try:
            detail = json.dumps(tool_input)
        except Exception:
            detail = ""
    detail = " ".join(str(detail).split())
    if len(detail) > 200:
        detail = detail[:200] + "…"
    return f"{tool_name}: {detail}" if detail else tool_name


def run_permission_hook(wait_secs: int) -> None:
    """
    PermissionRequest hook mode (--hook permission): forward the prompt to the
    daemon and block until someone approves/denies remotely or the daemon
    times out. Prints the Claude Code decision JSON on allow/deny; prints
    nothing otherwise, which makes Claude Code show its normal built-in prompt.
    """
    raw = ""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    session_id = (data.get("session_id") or data.get("sessionId") or "unknown")
    tool_name  = data.get("tool_name") or "?"
    tool_input = data.get("tool_input") or {}

    # Tools whose real interaction is answered locally (a multiple-choice / free
    # text answer, not an allow/deny) can't be handled remotely — the hook can
    # only allow/deny. (Verified: neither exit-2+stderr nor a JSON deny+reason
    # feeds the chosen answer back to Claude.) Let them fall straight through to
    # Claude Code's own UI instead of raising a useless "Allow?" prompt.
    if tool_name in ("AskUserQuestion",):
        return

    # ExitPlanMode carries the full plan (markdown) in tool_input — keep it
    # readable on the client instead of clipping it to the default 500 chars.
    trunc = (_truncate_tool_input(tool_input, max_str=8000, max_total=12000)
             if tool_name == "ExitPlanMode"
             else _truncate_tool_input(tool_input))
    request = {
        "type":       "permission_request",
        "session_id": session_id,
        "prompt_id":  data.get("prompt_id") or uuid.uuid4().hex,
        "tool_name":  tool_name,
        "summary":    _tool_summary(tool_name, tool_input),
        "tool_input": trunc,
        "cwd":        data.get("cwd", ""),
    }

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(request) + "\n").encode())

        # The daemon always replies (decision or null at its own timeout);
        # the extra headroom only matters if the daemon misbehaves.
        sock.settimeout(HOOK_WAIT_CEILING)
        buf = b""
        while b"\n" not in buf and len(buf) < 4096:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        sock.close()

        decision = json.loads(buf.decode()).get("decision") if buf.strip() else None
        if decision in ("allow", "deny"):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": decision},
                }
            }))
        return
    except Exception:
        pass

    # Daemon unreachable: behave like the plain status hook so the session
    # still shows as waiting, and let Claude Code prompt normally.
    try:
        os.makedirs(MONITOR_STATE_DIR, exist_ok=True)
        with open(os.path.join(MONITOR_STATE_DIR, f"{session_id}.json"), "w") as f:
            json.dump({"state": "waiting", "time": time.time(), "session_id": session_id}, f)
    except Exception:
        pass


def run_question_hook(wait_secs: int) -> None:
    """
    PreToolUse hook mode (--hook question, matcher AskUserQuestion): forward the
    question(s) to the daemon and block for a remote answer. On success prints a
    PreToolUse updatedInput that carries the original tool_input plus an
    `answers` map, which makes Claude Code use the answer WITHOUT showing its
    dialog. Prints nothing on timeout/disabled/unreachable → the local dialog
    appears (safe fallback).
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    tool_input = data.get("tool_input") or {}
    questions  = tool_input.get("questions") or []
    if not questions:
        return   # nothing to answer → fall through

    request = {
        "type":       "question_request",
        "session_id": data.get("session_id") or data.get("sessionId") or "unknown",
        "prompt_id":  data.get("prompt_id") or uuid.uuid4().hex,
        "questions":  questions,
        "cwd":        data.get("cwd", ""),
    }

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(request) + "\n").encode())

        sock.settimeout(HOOK_WAIT_CEILING)
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        sock.close()

        answers = json.loads(buf.decode()).get("answers") if buf.strip() else None
        if isinstance(answers, dict) and answers:
            # updatedInput REPLACES tool_input, so keep the original questions
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {**tool_input, "answers": answers},
                }
            }))
    except Exception:
        pass
    # Any failure → print nothing → Claude Code shows its own dialog.

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
        # Absolute reset instants (epoch seconds) so offline clients can keep
        # counting down and zero the bar once the window has passed.
        "session_reset_at": _epoch(session.get("resets_at", "")),
        "weekly_reset_at":  _epoch(weekly.get("resets_at",  "")),
    }

# ── Payload helpers ───────────────────────────────────────────────────────────

_STATUS_COLOR = {
    "working":  "\033[33m",   # orange
    "waiting":  "\033[31m",   # red
    "idle": "\033[32m",   # green
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
            # Challenge-response: the client proves it knows the secret by
            # returning HMAC-SHA256(secret, nonce), so the secret itself never
            # crosses the (plaintext ws://) wire. Legacy clients that send the
            # secret directly are still accepted during the transition.
            try:
                nonce = secrets.token_hex(16)
                await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)
                expected = hmac.new(secret.encode(), nonce.encode(),
                                    hashlib.sha256).hexdigest()
                if "auth_hmac" in data:
                    ok = hmac.compare_digest(str(data.get("auth_hmac", "")), expected)
                elif "auth" in data:
                    ok = hmac.compare_digest(str(data.get("auth", "")), secret)  # legacy
                else:
                    ok = False
                if not ok:
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
            # Push timezone offset so the screen can configure NTP correctly,
            # and tell clients whether remote-control (permissions/questions/
            # conversation) is armed so the app can show its control tabs.
            utc_offset = int(datetime.now().astimezone().utcoffset().total_seconds())
            await ws.send(json.dumps({"type": "config", "utc_offset": utc_offset,
                                      "remote_control": _remote_permissions}))

            # Send current state immediately so the client isn't blank on connect
            sessions, status = _overall_status()
            with _lock:
                usage = dict(_usage_cache)
            await ws.send(json.dumps({**usage, "sessions": sessions, "status": status}))

            client_name = "ws"
            try:
                async for raw in ws:
                    try:
                        m = json.loads(raw)
                    except Exception:
                        continue
                    mtype = m.get("type")

                    if mtype == "subscribe":
                        client_name = str(m.get("client") or "ws")
                        feats = m.get("features") or []
                        # A client that wants permissions and/or questions joins
                        # the remote-control subscriber set and gets both replays.
                        wants = (("permissions" in feats and _remote_permissions)
                                 or ("questions" in feats and _remote_questions))
                        if wants:
                            _perm_clients.add(ws)
                            # Track question-answering clients separately so the
                            # fall-through gate knows if anyone can answer.
                            if "questions" in feats and _remote_questions:
                                _question_clients.add(ws)
                            _log(f"[ws] remote-control subscriber: {client_name}")
                            with _lock:
                                pending = ([_perm_request_payload(e) for e in _pending_perms.values()]
                                           + [_question_request_payload(e) for e in _pending_questions.values()])
                            for p in pending:
                                await ws.send(json.dumps(p))
                        # Conversation feed: gated on remote-control being armed.
                        if "conversation" in feats and _remote_permissions:
                            _conv_clients.add(ws)
                            _log(f"[ws] conversation subscriber: {client_name}")
                            snapshot = _conversation_payload()
                            if snapshot is not None:
                                await ws.send(json.dumps(snapshot))

                    elif mtype == "permission_response":
                        rid      = str(m.get("id", ""))
                        decision = str(m.get("decision", ""))
                        if _resolve_permission(rid, decision, client_name):
                            _log(f"[perm] {decision} by {client_name}")

                    elif mtype == "question_response":
                        rid     = str(m.get("id", ""))
                        answers = m.get("answers")
                        if _resolve_question(rid, answers, client_name):
                            _log(f"[question] answered by {client_name}")

                    elif mtype == "extend":
                        _extend_request(str(m.get("id", "")))
            except Exception:
                pass  # connection reset without close frame — normal on app restart
        finally:
            _ws_clients.discard(ws)
            _perm_clients.discard(ws)
            _conv_clients.discard(ws)
            if ws in _question_clients:
                _note_qclient_gone()
            _question_clients.discard(ws)
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
                conn.settimeout(2.0)
                raw = b""
                while b"\n" not in raw and len(raw) < 8192:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                msg = json.loads(raw.decode())

                if msg.get("type") == "permission_request":
                    _register_permission(conn, msg)   # takes ownership of conn
                    conn = None
                    continue
                if msg.get("type") == "question_request":
                    _register_question(conn, msg)     # takes ownership of conn
                    conn = None
                    continue

                sid   = msg.get("session_id", "unknown")
                state = msg.get("state", "")

                if state:
                    _update_session(sid, state,
                                    transcript=msg.get("transcript_path", ""),
                                    cwd=msg.get("cwd", ""))
                    # Any post-request activity in the session (PostToolUse or
                    # PermissionDenied → "working", SessionEnd → "ended") means
                    # the prompt was answered in Claude Code's own dialog —
                    # resolve our pending request so remote prompts dismiss.
                    if state in ("working", "ended"):
                        _cancel_permissions_for(sid)
                    vprint(f"[socket] {sid[:8]}… → {state}")
                    _push()
                    # The transcript just grew — refresh the conversation feed.
                    _broadcast_conversation()
            except Exception as e:
                vprint(f"[socket] error: {e}")
            finally:
                if conn is not None:
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

    uninstall_vscode_extension()

    print("[uninstall] done")


# ── Systemd service install ───────────────────────────────────────────────────

# CLI name → user settings.json path (Linux)
_VSCODE_FLAVORS = [
    ("code",          "~/.config/Code/User/settings.json"),
    ("code-insiders", "~/.config/Code - Insiders/User/settings.json"),
    ("codium",        "~/.config/VSCodium/User/settings.json"),
]
_VSCODE_EXT_ID = "sensnology.codelight"


def _find_vscode_cli() -> tuple[str, str] | None:
    """Return (cli_path, settings_path) for the first VSCode flavor found."""
    for cli, settings in _VSCODE_FLAVORS:
        exe = shutil.which(cli)
        if exe:
            return exe, os.path.expanduser(settings)
    return None


def _configure_vscode_settings(settings_path: str, secret: str, ws_port: int) -> None:
    """Write codelight.* keys into the VSCode user settings. VSCode reloads
    settings.json live, so the extension picks this up without a restart."""
    settings = {}
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        pass
    except Exception:
        # settings.json may legally contain comments (JSONC) — never risk
        # clobbering a file we can't round-trip
        print(f"[vscode] could not parse {settings_path} (comments?) — set "
              f"codelight.secret = {secret!r} manually", file=sys.stderr)
        return

    desired = {"codelight.secret": secret}
    if ws_port != 8765:
        desired["codelight.port"] = ws_port
    if all(settings.get(k) == v for k, v in desired.items()):
        return
    settings.update(desired)

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=4)
        f.write("\n")
    print(f"[vscode] configured codelight.secret in {settings_path}")


def _find_local_vsix() -> str | None:
    """A repo checkout with a freshly built .vsix beats downloading."""
    import glob
    ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "vscode-extension")
    candidates = sorted(glob.glob(os.path.join(ext_dir, "codelight-*.vsix")),
                        key=os.path.getmtime)
    return candidates[-1] if candidates else None


def install_vscode_extension(secret: str = "", ws_port: int = 8765) -> None:
    """Install the codelight VSCode extension (local build or latest GitHub
    release) and configure its settings to match this daemon."""
    import subprocess

    found = _find_vscode_cli()
    release_url = "https://github.com/henrikekblad/codelight/releases"
    if found is None:
        print("[vscode] 'code' CLI not found — install the extension manually:",
              file=sys.stderr)
        print(f"[vscode]   download codelight-*.vsix from {release_url}", file=sys.stderr)
        print("[vscode]   then: code --install-extension <file.vsix>", file=sys.stderr)
        return
    code, settings_path = found

    vsix_path = _find_local_vsix()
    if vsix_path:
        print(f"[vscode] using local build {os.path.basename(vsix_path)}")
    else:
        try:
            api = "https://api.github.com/repos/henrikekblad/codelight/releases/latest"
            with urllib.request.urlopen(api, timeout=15) as r:
                release = json.load(r)
            asset = next((a for a in release.get("assets", [])
                          if a.get("name", "").endswith(".vsix")), None)
            if asset is None:
                print(f"[vscode] no .vsix asset in the latest release — see {release_url}",
                      file=sys.stderr)
                return
            cache = os.path.expanduser("~/.cache/codelight")
            os.makedirs(cache, exist_ok=True)
            vsix_path = os.path.join(cache, asset["name"])
            print(f"[vscode] downloading {asset['name']}…")
            urllib.request.urlretrieve(asset["browser_download_url"], vsix_path)
        except Exception as e:
            print(f"[vscode] could not download extension: {e}", file=sys.stderr)
            return

    try:
        result = subprocess.run([code, "--install-extension", vsix_path, "--force"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[vscode] install failed: {result.stderr.strip()}", file=sys.stderr)
            return
        print(f"[vscode] extension installed ({os.path.basename(vsix_path)})")
    except Exception as e:
        print(f"[vscode] could not install extension: {e}", file=sys.stderr)
        return

    if secret:
        _configure_vscode_settings(settings_path, secret, ws_port)


def uninstall_vscode_extension() -> None:
    """Remove the extension and its settings from every VSCode flavor present."""
    import subprocess

    for cli, settings in _VSCODE_FLAVORS:
        exe = shutil.which(cli)
        if not exe:
            continue
        try:
            listed = subprocess.run([exe, "--list-extensions"],
                                    capture_output=True, text=True)
            if _VSCODE_EXT_ID in listed.stdout:
                subprocess.run([exe, "--uninstall-extension", _VSCODE_EXT_ID],
                               capture_output=True, text=True)
                print(f"[vscode] extension removed from {cli}")
        except Exception:
            pass

        settings_path = os.path.expanduser(settings)
        try:
            with open(settings_path) as f:
                data = json.load(f)
            cleaned = {k: v for k, v in data.items() if not k.startswith("codelight.")}
            if cleaned != data:
                with open(settings_path, "w") as f:
                    json.dump(cleaned, f, indent=4)
                    f.write("\n")
                print(f"[vscode] settings cleaned in {settings_path}")
        except Exception:
            pass


def install_service(name: str, secret: str, ws_port: int, verbose: bool,
                    remote_control: bool = False,
                    permission_timeout: int = 60) -> None:
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
    if remote_control:
        args_line += " --remote-control"
        if permission_timeout != 60:
            args_line += f" --permission-timeout {permission_timeout}"

    unit = f"""\
[Unit]
Description=Claude Code status monitor
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={python_path} -u {script_path} {args_line}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""

    service_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(service_dir, exist_ok=True)
    service_path = os.path.join(service_dir, "codelight.service")

    with open(service_path, "w") as f:
        f.write(unit)
    print(f"[install] wrote {service_path}")

    for cmd in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "reenable", "codelight"],  # re-link under the current WantedBy target
        ["systemctl", "--user", "restart", "codelight"],   # replace an already-running old instance
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
    parser.add_argument("--remote-control", "--remote-permissions", dest="remote_control",
                        action="store_true",
                        help="Let clients (Android/GNOME) remotely approve Claude Code "
                             "permission prompts AND answer AskUserQuestion prompts. "
                             "Requires --secret. (--remote-permissions is a deprecated alias.)")
    parser.add_argument("--permission-timeout", type=int, default=60,
                        help="Seconds to wait for a remote decision/answer before "
                             "falling back to Claude Code's built-in prompt (default: 60)")
    parser.add_argument("--vscode", action="store_true",
                        help="With --install: also install the codelight VSCode "
                             "extension from the latest GitHub release")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
        return

    if args.install:
        if args.name is None:
            parser.error("--name is required with --install")
        if args.remote_control and not args.secret:
            parser.error("--remote-control requires --secret (remote approval/answers "
                         "are code-execution capability and must not be open to the LAN)")
        install_service(args.name, args.secret, args.ws_port, args.verbose,
                        args.remote_control, args.permission_timeout)
        if args.vscode:
            install_vscode_extension(args.secret, args.ws_port)
        return

    if args.hook == "permission":
        run_permission_hook(args.permission_timeout)
        return
    if args.hook == "question":
        run_question_hook(args.permission_timeout)
        return
    if args.hook:
        run_hook(args.hook)
        return

    if args.name is None:
        parser.error("--name is required (e.g. --name henrik-laptop). "
                     "It identifies this daemon to clients.")

    _verbose = args.verbose

    global _remote_permissions, _remote_questions, _permission_timeout
    _permission_timeout = args.permission_timeout
    _remote_permissions = args.remote_control
    _remote_questions   = args.remote_control
    if args.remote_control and not args.secret:
        print("[rc] --remote-control requires --secret — feature disabled",
              file=sys.stderr, flush=True)
        _remote_permissions = _remote_questions = False

    install_hooks(os.path.abspath(__file__), _remote_permissions,
                  _remote_questions, _permission_timeout)

    print(f"codelight  [ws://0.0.0.0:{args.ws_port}]  (Ctrl-C to stop)", flush=True)

    threading.Thread(target=_socket_thread, daemon=True).start()
    threading.Thread(target=_usage_thread,  daemon=True).start()
    threading.Thread(target=_conv_poll_thread, daemon=True).start()

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
