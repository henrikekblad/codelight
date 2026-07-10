#!/usr/bin/env python3
"""
codelight.py - pushes coding-agent status to codelight clients.

Usage:
    python3 codelight.py --name my-laptop
    python3 codelight.py dashboard
    python3 codelight.py --name my-laptop --verbose   # also show socket events and API data
    python3 -u codelight.py | tee                         # -u avoids buffering when piping
"""
import argparse
import asyncio
import collections
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from codelight_core.agents import claude as claude_agent
from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent
from codelight_core import auth as auth_core
from codelight_core.conversation import ConversationRefresher
from codelight_core import dashboard_client
from codelight_core import discovery as discovery_core
from codelight_core import hook_io
from codelight_core import hooks as hooks_core
from codelight_core import hook_runtime
from codelight_core import policy as policy_core
from codelight_core import remote_control
from codelight_core import remote_payloads
from codelight_core import service as service_core
from codelight_core.state import CodelightState
from codelight_core import transcript as transcript_core
from codelight_core import timefmt
from codelight_core.usage import UsageFetchers, UsagePoller
from codelight_core import vscode as vscode_core

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
COPILOT_HOME      = os.path.expanduser(os.environ.get("COPILOT_HOME", "~/.copilot"))
CODEX_HOME        = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
CODELIGHT_CONFIG_HOME = os.path.expanduser(
    os.environ.get("CODELIGHT_CONFIG_HOME", "~/.config/codelight"))
POLICY_PATH       = os.path.join(CODELIGHT_CONFIG_HOME, "policy.json")
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
_policy_lock: threading.Lock = threading.Lock()
# session_id → {"state": "working"|"waiting", "time": float}
_github_org: str = ""
_github_token_file: str = ""

_ws_loop:    asyncio.AbstractEventLoop | None = None
_ws_clients: set = set()
_last_ws_status: str = "idle"   # updated by _broadcast; watched by timeout-watchdog
_dbus_iface: object | None = None   # CodelightDbusInterface instance when D-Bus is available

# Remote control (armed via --remote-control, requires --secret):
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
_log_lines:       collections.deque = collections.deque(maxlen=10)
_conversation_refresher: ConversationRefresher | None = None

AGENT_REGISTRY: dict[str, dict[str, str]] = {
    "claude": {"display": "Claude"},
    "copilot": {"display": "Copilot"},
    "codex": {"display": "Codex"},
}
DEFAULT_AGENT_ID = "claude"
_state = CodelightState(
    default_agent_id=DEFAULT_AGENT_ID,
    agent_registry=AGENT_REGISTRY,
    idle_window=IDLE_WINDOW,
    idle_window_waiting=IDLE_WINDOW_WAITING,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def vprint(*args, **kwargs):
    if _verbose:
        print(*args, **kwargs, flush=True)


def _log(msg: str) -> None:
    """Append a timestamped line to the rolling activity log.
    The terminal dashboard consumes this over the same client payload as every
    other surface."""
    ts = datetime.now().strftime("%H:%M:%S")
    _log_lines.append(f"[{ts}] {msg}")
    print(f"[{ts}] {msg}", flush=True)


def _format_countdown(diff_secs: int) -> str:
    return timefmt.format_countdown(diff_secs)


def _epoch(iso_ts: str) -> int:
    """ISO-8601 timestamp → epoch seconds (0 if unparseable)."""
    return timefmt.epoch(iso_ts)


def _format_iso_countdown(iso_ts: str) -> str:
    """Convert an ISO-8601 timestamp to a human-readable countdown like '3h 45m'."""
    return timefmt.format_iso_countdown(iso_ts)


def _format_epoch_countdown(epoch_seconds: int) -> str:
    return timefmt.format_epoch_countdown(epoch_seconds)


def _normalize_agent_id(agent_id: str | None) -> str:
    return _state.normalize_agent_id(agent_id)


def _agent_display_name(agent_id: str | None) -> str:
    return _state.agent_display_name(agent_id)


def _agent_meter_titles(agent_id: str | None) -> tuple[str, str]:
    return _state.agent_meter_titles(agent_id)


def _valid_auth_response(data: dict, secret: str, nonce: str) -> bool:
    if not isinstance(data, dict) or "auth_hmac" not in data:
        return False
    return auth_core.valid_auth_response(data, secret, nonce)


def _get_local_ip() -> str:
    return discovery_core.get_local_ip()


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
                    transcript: str = "", cwd: str = "",
                    agent_id: str = DEFAULT_AGENT_ID) -> None:
    normalized_agent = _normalize_agent_id(agent_id)
    if not transcript and normalized_agent == "copilot":
        transcript = _copilot_events_path_for_session(session_id)
    elif not transcript and normalized_agent == "codex":
        transcript = _codex_rollout_path_for_session(session_id)
    _state.update_session(
        session_id,
        state,
        transcript=transcript,
        cwd=cwd,
        agent_id=normalized_agent,
    )


def _active_transcript() -> tuple[str, str]:
    """(session_id, transcript_path) of the most-recently-active session that
    has a known transcript. Falls back to the last transcript we ever saw so
    the trailing message survives the session being popped on Stop."""
    active = _state.active_transcript()
    if active.path:
        return (active.session_id, active.path)
    # Copilot fallback: if hooks did not pass transcript_path, use the newest
    # session-state events file.
    p = _latest_copilot_events_path()
    if p:
        return ("copilot", p)
    return ("", "")


def _copilot_events_path_for_session(session_id: str) -> str:
    return copilot_agent.events_path_for_session(COPILOT_HOME, session_id)


def _latest_copilot_events_path() -> str:
    return copilot_agent.latest_events_path(COPILOT_HOME)


def _codex_rollout_path_for_session(session_id: str) -> str:
    return codex_agent.rollout_path_for_session(CODEX_HOME, session_id)


def _latest_codex_rollout_path() -> str:
    return codex_agent.latest_rollout_path(CODEX_HOME)


def _parse_transcript(path: str, max_msgs: int = 60) -> list[dict]:
    return transcript_core.parse_transcript(
        path, tool_summary=_tool_summary, max_msgs=max_msgs)


def _extract_transcript_path(data: dict) -> str:
    """Read transcript path across hook payload variants."""
    return transcript_core.extract_transcript_path(data)


def _is_noise(s: str) -> bool:
    """True for machine-generated wrappers (slash-commands, IDE hints, injected
    reminders) that aren't turns the human actually typed."""
    return transcript_core.is_noise(s)


def _tool_result_text(content) -> str:
    """Extract a short plain-text snippet from a tool_result block's content."""
    return transcript_core.tool_result_text(content)


def _codex_tool_result_text(content) -> str:
    """Remove Codex's execution envelope from a tool result."""
    return transcript_core.codex_tool_result_text(content)


def _conversation_payload() -> dict | None:
    """Build the {"type":"conversation", ...} feed for the active session."""
    sid, path = _active_transcript()
    if not path:
        return None
    agent_id = _state.conversation_agent(sid)
    lines = _parse_transcript(path)
    for line in lines:
        if isinstance(line, dict):
            line.setdefault("agent_id", agent_id)
            line.setdefault("agent_display", _agent_display_name(agent_id))
    return {
        "type": "conversation",
        "session_id": sid,
        "agent_id": agent_id,
        "agent_display": _agent_display_name(agent_id),
        "lines": lines,
    }


def _active_conversation_path() -> str:
    return _active_transcript()[1]


def _has_conversation_clients() -> bool:
    return bool(_conv_clients)


def _conversation_refresh_thread() -> None:
    if _conversation_refresher is not None:
        _conversation_refresher.run()


def _notify_conversation_changed() -> None:
    if _conversation_refresher is not None:
        _conversation_refresher.notify()


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


def _status_rank(status: str) -> int:
    return CodelightState._status_rank(status)


def _overall_status() -> tuple[int, str, dict[str, str], str]:
    """Return (active_count, overall_status) from in-memory session state.
    Cleans up sessions that have been silent longer than IDLE_WINDOW."""
    with _lock:
        # Sessions with a pending remote permission/question request stay alive —
        # the 30 s waiting window would otherwise drop them mid-request
        pending_sids = ({p["session_id"] for p in _pending_perms.values()}
                        | {q["session_id"] for q in _pending_questions.values()})
    return _state.overall_status(pending_sids)


def _status_snapshot() -> dict:
    with _lock:
        pending_sids = ({p["session_id"] for p in _pending_perms.values()}
                        | {q["session_id"] for q in _pending_questions.values()})
    payload = _state.status_snapshot(pending_sids)
    payload["activity"] = list(_log_lines)
    payload["clients"] = {
        "websocket": len(_ws_clients),
        "dbus": _dbus_iface is not None,
    }
    return payload


def _usage_limit(label: str, usage: dict, prefix: str) -> dict:
    """Return the generic limit shape understood by multi-agent clients."""
    return CodelightState._usage_limit(label, usage, prefix)

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
            return json.dumps(_status_snapshot())

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
    cwd = str(entry.get("cwd") or "")
    can_allow_folder = bool(cwd) and (not _is_trusted_repo_cwd(cwd))
    can_allow_command = bool(cwd) and bool(entry.get("policy_command"))
    return remote_payloads.permission_request_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        agent_display_name=_agent_display_name,
        allow_folder_available=can_allow_folder,
        allow_command_available=can_allow_command,
    )


def _resolve_permission(request_id: str, decision: str, by: str) -> bool:
    """Record a decision for a pending request. First response wins."""
    if decision not in ("allow", "deny", "skip", "allow_folder", "allow_command"):
        return False

    if decision in ("allow_folder", "allow_command"):
        with _lock:
            entry = _pending_perms.get(request_id)
            if entry is None or entry["decision"] is not None or entry["by"] is not None:
                return False
            cwd = str(entry.get("cwd") or "")
            policy_command = str(entry.get("policy_command") or "")

        if decision == "allow_folder":
            persisted, value = _allow_folder(cwd)
            kind = "folder"
        else:
            persisted, value = _allow_command(policy_command, cwd)
            kind = "command"

        with _lock:
            entry = _pending_perms.get(request_id)
            if entry is None or entry["decision"] is not None or entry["by"] is not None:
                return False
            entry["decision"] = "allow"
            entry["by"] = by
            entry["persistence"] = {
                "kind": kind,
                "requested": True,
                "persisted": persisted,
                "value": value,
            }
        entry["event"].set()
        return True

    with _lock:
        entry = _pending_perms.get(request_id)
        if entry is None or entry["decision"] is not None or entry["by"] is not None:
            return False
        entry["decision"] = None if decision == "skip" else decision
        entry["by"] = by
    entry["event"].set()
    return True


def _wait_with_extend(entry: dict) -> None:
    """Block until the request is resolved (event set) or its deadline passes.
    Re-reads entry['expires'] each loop so a client keepalive (_extend_request)
    can push the deadline out while a human is still interacting."""
    remote_control.wait_with_extend(entry)


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
    remote_control.wait_question(
        entry,
        can_answer_questions=_can_answer_questions,
        last_client_gone=lambda: _last_qclient_gone,
        no_client_grace=NO_CLIENT_GRACE,
        reconnect_window=RECONNECT_WINDOW,
    )


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


def _should_cancel_pending_for_hook(state: str, hook_event: str) -> bool:
    """Whether a lifecycle event proves a local prompt is no longer pending."""
    return remote_control.should_cancel_pending_for_hook(state, hook_event)


def _cancel_pending_for_hook(session_id: str, state: str,
                             hook_event: str) -> bool:
    if not _should_cancel_pending_for_hook(state, hook_event):
        return False
    _cancel_permissions_for(session_id)
    return True


def _permission_waiter(entry: dict) -> None:
    """Per-request thread: wait for a decision (or timeout), reply to the
    blocked hook on its held connection, and notify clients."""
    _wait_with_extend(entry)
    with _lock:
        _pending_perms.pop(entry["id"], None)
        decision = entry["decision"]
        by       = entry["by"]
        persistence = entry.get("persistence") \
            if isinstance(entry.get("persistence"), dict) else None

    try:
        entry["conn"].sendall((json.dumps({"decision": decision}) + "\n").encode())
    except Exception:
        pass
    try:
        entry["conn"].close()
    except Exception:
        pass

    outcome = decision or ("cancelled" if by == "cancelled" else "skip" if by else "timeout")
    if decision == "allow" and persistence and persistence.get("requested"):
        outcome = f"allow_{persistence.get('kind', 'once')}"
    _log(f"[perm] {entry['summary'][:60]} → {outcome}"
         + (f" (by {by})" if decision else ""))
    _broadcast_rc(remote_payloads.permission_resolved_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        decision=outcome,
        by=by or "",
        persistence=persistence,
        agent_display_name=_agent_display_name,
    ), "PermissionResolved")
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
        "agent_id":   _normalize_agent_id(msg.get("agent_id")),
        "tool_name":  msg.get("tool_name", "?"),
        "summary":    msg.get("summary", "") or msg.get("tool_name", "?"),
        "tool_input": msg.get("tool_input", {}),
        "policy_command": msg.get("policy_command", ""),
        "cwd":        msg.get("cwd", ""),
        "event":      threading.Event(),
        "decision":   None,
        "by":         None,
        "expires":    time.time() + _permission_timeout,
    }
    with _lock:
        _pending_perms[rid] = entry
    _update_session(sid, "waiting", agent_id=entry["agent_id"])
    _log(f"[perm] request: {entry['summary'][:60]}")
    _push()
    _broadcast_rc(_perm_request_payload(entry), "PermissionRequest")
    threading.Thread(target=_permission_waiter, args=(entry,), daemon=True).start()


# ── Remote question answering via PreToolUse ──────────────────────────────────

def _question_request_payload(entry: dict) -> dict:
    return remote_payloads.question_request_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        agent_display_name=_agent_display_name,
    )


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
    _broadcast_rc(remote_payloads.question_resolved_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        by=by or "",
        agent_display_name=_agent_display_name,
    ), "QuestionResolved")
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
        "agent_id":   _normalize_agent_id(msg.get("agent_id")),
        "questions":  msg.get("questions", []),
        "cwd":        msg.get("cwd", ""),
        "event":      threading.Event(),
        "answers":    None,
        "by":         None,
        "expires":    time.time() + _permission_timeout,
    }
    with _lock:
        _pending_questions[rid] = entry
    _update_session(sid, "waiting", agent_id=entry["agent_id"])
    _log(f"[question] request: {len(entry['questions'])} question(s)")
    _push()
    _broadcast_rc(_question_request_payload(entry), "QuestionRequest")
    threading.Thread(target=_question_waiter, args=(entry,), daemon=True).start()


# ── Hook installation ─────────────────────────────────────────────────────────

HookSpec = hooks_core.HookSpec


def _hook_command_base(script_path: str, agent_id: str) -> str:
    return hooks_core.hook_command_base(script_path, agent_id)


def _command_hook(command: str, timeout_key: str = "timeout",
                  timeout: int | None = None,
                  status_message: str | None = None) -> dict:
    return hooks_core.command_hook(
        command, timeout_key=timeout_key, timeout=timeout,
        status_message=status_message)


def _is_codelight_hook_cmd(cmd: str) -> bool:
    return hooks_core.is_codelight_hook_cmd(cmd)


def _read_json_object(path: str, label: str) -> dict | None:
    return hooks_core.read_json_object(path, label)


def _write_json_object(path: str, data: dict) -> None:
    hooks_core.write_json_object(path, data)


def _merge_matcher_group_hooks(hooks: dict, desired: list[HookSpec]) -> bool:
    return hooks_core.merge_matcher_group_hooks(hooks, desired)


def _install_matcher_group_hooks(path: str, desired: list[HookSpec],
                                 label: str) -> None:
    hooks_core.install_matcher_group_hooks(path, desired, label, vprint=vprint)


def _remove_matcher_group_hooks(path: str) -> None:
    hooks_core.remove_matcher_group_hooks(path)


def install_hooks(script_path: str, remote_permissions: bool = False,
                  remote_questions: bool = False, permission_timeout: int = 60) -> None:
    """
    Ensure ~/.claude/settings.json has the monitor hooks pointing to this script.
    Idempotent: safe to call on every startup. Preserves all non-monitor hooks.
    With remote_permissions the PermissionRequest hook blocks for a remote
    decision; with remote_questions a PreToolUse hook (matcher AskUserQuestion)
    blocks for a remote answer.
    """
    hooks_core.install_claude_hooks(
        os.path.expanduser("~/.claude/settings.json"),
        script_path,
        hook_wait_ceiling=HOOK_WAIT_CEILING,
        remote_permissions=remote_permissions,
        remote_questions=remote_questions,
        permission_timeout=permission_timeout,
        vprint=vprint,
    )


def _copilot_hooks_path() -> str:
    return hooks_core.copilot_hooks_path(COPILOT_HOME)


def _codex_hooks_path() -> str:
    return hooks_core.codex_hooks_path(CODEX_HOME)


def install_codex_hooks(script_path: str, remote_permissions: bool = False,
                        remote_questions: bool = False,
                        permission_timeout: int = 60) -> None:
    """Install user-level Codex hooks in ~/.codex/hooks.json.

    Codex local surfaces (CLI and IDE extension) share CODEX_HOME. Project-local
    hooks would need trust per repo, so codelight uses the user layer.
    """
    hooks_core.install_codex_hooks(
        _codex_hooks_path(),
        script_path,
        hook_wait_ceiling=HOOK_WAIT_CEILING,
        remote_permissions=remote_permissions,
        remote_questions=remote_questions,
        permission_timeout=permission_timeout,
        vprint=vprint,
    )


def install_copilot_hooks(script_path: str, remote_permissions: bool = False,
                          permission_timeout: int = 60) -> None:
    """Install user-level GitHub Copilot CLI hooks in ~/.copilot/hooks/codelight.json."""
    hooks_core.install_copilot_hooks(
        _copilot_hooks_path(),
        script_path,
        hook_wait_ceiling=HOOK_WAIT_CEILING,
        remote_permissions=remote_permissions,
        permission_timeout=permission_timeout,
    )

# ── Hook mode ─────────────────────────────────────────────────────────────────

def run_hook(state: str, agent_id: str = DEFAULT_AGENT_ID) -> None:
    """
    Hook mode: invoked by coding-agent hooks via --hook STATE.
    Fast path: sends event to the running daemon over the Unix socket (~1 ms).
    Fallback: writes a state file if the daemon is not running.
    Must exit immediately so it never blocks the host agent.
    """
    data = hook_runtime.parse_json_object(sys.stdin.read())
    session_id = hook_runtime.session_id(data)
    transcript_path = _extract_transcript_path(data)
    hook_event = hook_runtime.hook_event_name(data)

    if hook_io.send_json(
        SOCKET_PATH,
        {
            "state": state,
            "session_id": session_id,
            "agent_id": _normalize_agent_id(agent_id),
            # Let the daemon tail this session's conversation for the app feed.
            "transcript_path": transcript_path,
            "cwd": data.get("cwd", ""),
            "hook_event": hook_event,
        },
        timeout=0.5,
    ):
        return

    hook_io.write_monitor_state(
        MONITOR_STATE_DIR,
        session_id=session_id,
        state=state,
        agent_id=_normalize_agent_id(agent_id),
        hook_event=hook_event,
    )


def _truncate_tool_input(tool_input, max_str: int = 500, max_total: int = 3000):
    return policy_core.truncate_tool_input(
        tool_input, max_str=max_str, max_total=max_total)


def _norm_path(path: str) -> str:
    return policy_core.norm_path(path)


def _load_policy() -> dict:
    return policy_core.load_policy(POLICY_PATH)


def _write_policy(policy: dict) -> bool:
    """Atomically persist the user-owned cross-agent permission policy."""
    return policy_core.write_policy(POLICY_PATH, policy)


def _trusted_folders() -> list[str]:
    """Return normalized trusted roots from codelight's policy."""
    return policy_core.trusted_folders(POLICY_PATH)


def _path_is_within(path: str, root: str) -> bool:
    return policy_core.path_is_within(path, root)


def _is_trusted_repo_cwd(cwd: str) -> bool:
    return policy_core.is_trusted_repo_cwd(POLICY_PATH, cwd)


def _repo_root_for(cwd: str) -> str:
    return policy_core.repo_root_for(cwd)


def _allow_folder(cwd: str) -> tuple[bool, str]:
    return policy_core.allow_folder(POLICY_PATH, _policy_lock, cwd)


def _command_from_tool(tool_name: str, tool_input) -> str:
    return policy_core.command_from_tool(tool_name, tool_input)


def _is_allowed_command(tool_name: str, tool_input, cwd: str) -> bool:
    return policy_core.is_allowed_command(POLICY_PATH, tool_name, tool_input, cwd)


def _allow_command(command: str, cwd: str) -> tuple[bool, str]:
    return policy_core.allow_command(POLICY_PATH, _policy_lock, command, cwd)


def _tool_summary(tool_name: str, tool_input: dict) -> str:
    return policy_core.tool_summary(tool_name, tool_input)


def _is_trusted_auto_allow_tool(tool_name: str) -> bool:
    return policy_core.is_trusted_auto_allow_tool(tool_name)


def _is_safe_memory_read(tool_name: str, tool_input) -> bool:
    return policy_core.is_safe_memory_read(tool_name, tool_input)


def _extract_patch_targets(patch_text: str) -> tuple[list[str], bool]:
    return policy_core.extract_patch_targets(patch_text)


def _is_trusted_target_path(path: str, cwd: str) -> bool:
    return policy_core.is_trusted_target_path(POLICY_PATH, path, cwd)


def _is_safe_trusted_apply_patch(tool_name: str, tool_input, cwd: str) -> bool:
    return policy_core.is_safe_trusted_apply_patch(
        POLICY_PATH, tool_name, tool_input, cwd)


def _emit_permission_decision(decision: str, copilot_mode: bool,
                              vscode_prettool_mode: bool,
                              reason: str = "") -> None:
    """Emit the host-specific decision envelope from one shared policy path."""
    print(json.dumps(hook_runtime.permission_decision_output(
        decision,
        copilot_mode=copilot_mode,
        vscode_prettool_mode=vscode_prettool_mode,
        reason=reason,
    )))


def run_permission_hook(wait_secs: int, copilot_mode: bool = False,
                        vscode_prettool_mode: bool = False,
                        agent_id: str | None = None) -> None:
    """
    PermissionRequest hook mode (--hook permission): forward the prompt to the
    daemon and block until someone approves/denies remotely or the daemon
    times out. Prints the Claude Code decision JSON on allow/deny; prints
    nothing otherwise, which makes Claude Code show its normal built-in prompt.
    """
    data = hook_runtime.parse_json_object(sys.stdin.read())
    session_id = hook_runtime.session_id(data)
    normalized_agent = _normalize_agent_id(
        agent_id or ("copilot" if (copilot_mode or vscode_prettool_mode) else "claude")
    )
    tool_name = hook_runtime.tool_name(data)
    tool_input = hook_runtime.tool_input(data)
    cwd = str(data.get("cwd") or "")

    # The shared ~/.copilot/hooks file is loaded by both Copilot CLI and VS Code.
    # Only VS Code's PreToolUse payload carries tool_use_id, so use it to keep
    # the extra PreToolUse hook a no-op under CLI sessions.
    if vscode_prettool_mode and not data.get("tool_use_id"):
        return

    # Question tools are answered through run_question_hook (updatedInput), not
    # this allow/deny path. Skip so PreToolUse question hooks can handle them.
    if hook_runtime.is_question_tool(tool_name, tool_input):
        return

    # Memory policy: allow only read-only view access for repo/session scopes.
    # Keep all mutating memory operations and user-global memory behind prompts.
    if _is_safe_memory_read(tool_name, tool_input):
        _emit_permission_decision(
            "allow", copilot_mode, vscode_prettool_mode,
            "Read-only memory view in repo/session scope")
        return

    # Exact commands explicitly approved for this repository are shared across
    # Claude, Copilot, and Codex. No prefix, regex, or shell parsing is used.
    if _is_allowed_command(tool_name, tool_input, cwd):
        _emit_permission_decision(
            "allow", copilot_mode, vscode_prettool_mode,
            "Exact command allowed by codelight policy")
        return

    # Trusted apply_patch edits: allow when all target files are in trusted
    # folders and the patch does not request file deletion.
    if _is_safe_trusted_apply_patch(tool_name, tool_input, cwd):
        _emit_permission_decision(
            "allow", copilot_mode, vscode_prettool_mode,
            "apply_patch target is within trusted codelight folder")
        return

    # Trusted-folder short-circuit: only allow explicit trust/read-only probes
    # under codelight policy roots. Mutating tools must still ask for approval.
    if _is_trusted_repo_cwd(cwd) and _is_trusted_auto_allow_tool(tool_name):
        _emit_permission_decision(
            "allow", copilot_mode, vscode_prettool_mode,
            "Read-only tool in trusted codelight folder")
        return

    # ExitPlanMode carries the full plan (markdown) in tool_input — keep it
    # readable on the client instead of clipping it to the default 500 chars.
    trunc = (_truncate_tool_input(tool_input, max_str=8000, max_total=12000)
             if tool_name == "ExitPlanMode"
             else _truncate_tool_input(tool_input))
    request = {
        "type":       "permission_request",
        "session_id": session_id,
        "agent_id":   normalized_agent,
        "agent_display": _agent_display_name(normalized_agent),
        "prompt_id":  data.get("prompt_id") or uuid.uuid4().hex,
        "tool_name":  tool_name,
        "summary":    _tool_summary(tool_name, tool_input),
        "tool_input": trunc,
        "policy_command": _command_from_tool(tool_name, tool_input),
        "cwd":        cwd,
    }

    try:
        response = hook_io.request_json(
            SOCKET_PATH,
            request,
            connect_timeout=2.0,
            response_timeout=HOOK_WAIT_CEILING,
            max_bytes=4096,
        )
        decision = response.get("decision") if response else None
        if decision in ("allow", "deny"):
            _emit_permission_decision(
                decision, copilot_mode, vscode_prettool_mode,
                "Denied by remote codelight approval" if decision == "deny" else "")
        return
    except Exception:
        pass

    # Daemon unreachable: behave like the plain status hook so the session
    # still shows as waiting, and let Claude Code prompt normally.
    try:
        hook_io.write_monitor_state(
            MONITOR_STATE_DIR,
            session_id=session_id,
            state="waiting",
            agent_id=normalized_agent,
        )
    except Exception:
        pass


def run_question_hook(wait_secs: int, vscode_prettool_mode: bool = False,
                      codex_context_mode: bool = False,
                      agent_id: str | None = None) -> None:
    """
    PreToolUse hook mode: forward question(s) to the daemon and block for a
        remote answer.
        - Default (Claude-style): emits PreToolUse updatedInput with answers.
        - VS Code/Codex context mode: emits PreToolUse deny + additionalContext
            so the native ask dialog/tool is skipped and the model still
            receives the answer.
        Prints nothing on timeout/disabled/unreachable → local dialog fallback.
    """
    normalized_agent = _normalize_agent_id(
        agent_id or ("copilot" if vscode_prettool_mode else "claude")
    )

    data = hook_runtime.parse_json_object(sys.stdin.read())
    tool_input = hook_runtime.tool_input(data)
    questions = hook_runtime.questions_from_input(data, tool_input)
    if not questions:
        return   # nothing to answer → fall through

    request = {
        "type":       "question_request",
        "session_id": hook_runtime.session_id(data),
        "agent_id":   normalized_agent,
        "agent_display": _agent_display_name(normalized_agent),
        "prompt_id":  data.get("prompt_id") or uuid.uuid4().hex,
        "questions":  questions,
        "cwd":        data.get("cwd", ""),
    }

    try:
        response = hook_io.request_json(
            SOCKET_PATH,
            request,
            connect_timeout=2.0,
            response_timeout=HOOK_WAIT_CEILING,
            max_bytes=65536,
        )
        answers = response.get("answers") if response else None
        if isinstance(answers, dict) and answers:
            if vscode_prettool_mode or codex_context_mode:
                print(json.dumps(hook_runtime.question_context_output(answers)))
            else:
                # VS Code/Copilot and Claude variants may look for different
                # override fields/shapes. Provide a rich replacement object so a
                # remote answer can short-circuit the native ask dialog.
                # updatedInput REPLACES tool_input for Claude-style hooks.
                # modifiedArgs is the equivalent shape used by Copilot CLI docs.
                print(json.dumps(
                    hook_runtime.question_updated_input_output(tool_input, answers)
                ))
    except Exception:
        pass
    # Any failure → print nothing → Claude Code shows its own dialog.

# ── Usage API ─────────────────────────────────────────────────────────────────
# Credentials are read fresh each poll so token rotations are picked up automatically.

_USAGE_API  = "https://claude.ai/api/oauth/usage"
_CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")


def _usage_fetchers() -> UsageFetchers:
    return UsageFetchers(
        claude_credentials_path=_CREDS_PATH,
        claude_usage_api=_USAGE_API,
        codex_home=CODEX_HOME,
        copilot_home=COPILOT_HOME,
        github_org=_github_org,
        github_token_file=_github_token_file,
        github_api=_github_api,
        log=vprint,
    )


def get_usage() -> dict | None:
    """
    Fetch usage from the claude.ai OAuth usage API.
    Returns a dict with session_pct/weekly_pct/resets, or None on failure
    (caller keeps cached values).
    """
    return _usage_fetchers().get_claude_usage()


def _usage_from_codex_rollout(path: str) -> dict | None:
    """Read the newest Codex 5-hour and weekly rate-limit snapshot."""
    return _usage_fetchers().codex_usage_from_rollout(path)


def get_codex_usage() -> dict | None:
    return _usage_fetchers().get_codex_usage()


def _github_token() -> str:
    """Resolve a GitHub token without making the gh CLI a requirement."""
    return _usage_fetchers().github_token()


def _github_api(path: str, token: str) -> dict:
    return copilot_agent.github_api(path, token)


def _next_month_start(now: datetime) -> int:
    return copilot_agent.next_month_start(now)


def get_copilot_usage(org: str | None = None, token: str | None = None,
                      now: datetime | None = None) -> dict | None:
    """Fetch the organization's pooled monthly Copilot AI-credit usage.

    Missing credentials, insufficient billing permission, and organizations
    without enhanced billing all return None. Clients then keep showing the
    Copilot activity status without inventing a zero-percent limit.
    """
    return _usage_fetchers().get_copilot_usage(org=org, token=token, now=now)


def _push() -> None:
    """Build payload from current state and broadcast to all clients."""
    payload = _status_snapshot()
    _broadcast(payload)


def run_dashboard(host: str, ws_port: int, secret: str) -> None:
    """Run the terminal dashboard as a normal WebSocket client."""
    if not _have_websockets:
        print("[dashboard] websockets not installed — dashboard unavailable",
              file=sys.stderr)
        print("[dashboard] Install: pip install websockets", file=sys.stderr)
        return
    uri = f"ws://{host}:{ws_port}"
    try:
        asyncio.run(dashboard_client.run(
            uri=uri,
            secret=secret,
            agent_registry=AGENT_REGISTRY,
            default_agent_id=DEFAULT_AGENT_ID,
            websockets_module=_websockets,
        ))
    except KeyboardInterrupt:
        pass

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
            # crosses the (plaintext ws://) wire.
            try:
                nonce = secrets.token_hex(16)
                await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                data = json.loads(msg)
                ok = _valid_auth_response(data, secret, nonce)
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
            await ws.send(json.dumps(_status_snapshot()))

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
                            _notify_conversation_changed()

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
                _, current_status, _, _ = _overall_status()
                if current_status != _last_ws_status:
                    _last_ws_status = current_status
                    payload = _status_snapshot()
                    msg = json.dumps(payload)
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
    if not _have_zeroconf:
        discovery_core.unavailable_message()
        return
    discovery_core.advertise_mdns(
        port=port,
        name=name,
        shutdown=_shutdown,
        zeroconf_cls=Zeroconf,
        service_info_cls=ServiceInfo,
        log=_log,
        verbose_log=vprint,
    )


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
                msg = hook_io.read_json_message(conn, max_bytes=8192)
                if msg is None:
                    continue

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
                    transcript_path = (
                        msg.get("transcript_path")
                        or msg.get("transcriptPath")
                        or msg.get("transcript")
                        or ""
                    )
                    _update_session(sid, state,
                                    transcript=transcript_path,
                                    cwd=msg.get("cwd", ""),
                                    agent_id=msg.get("agent_id", DEFAULT_AGENT_ID))
                    # PreToolUse status and question hooks may run concurrently.
                    # Only completion events prove a local prompt is finished.
                    _cancel_pending_for_hook(
                        sid, state, str(msg.get("hook_event") or ""))
                    vprint(f"[socket] {sid[:8]}… → {state}")
                    _push()
                    # The transcript just grew — refresh the conversation feed.
                    _notify_conversation_changed()
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
    """Refresh supported-agent usage and broadcast after each update."""
    UsagePoller(
        state=_state,
        fetch_claude=get_usage,
        fetch_codex=get_codex_usage,
        fetch_copilot=get_copilot_usage,
        interval=USAGE_INTERVAL,
        shutdown=_shutdown,
        log=_log,
        push=_push,
    ).run()

# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall() -> None:
    """Remove all codelight hooks, socket file, and state directory."""

    settings_path = os.path.expanduser("~/.claude/settings.json")
    _remove_matcher_group_hooks(settings_path)
    _remove_matcher_group_hooks(_codex_hooks_path())

    copilot_hooks = _copilot_hooks_path()
    try:
        os.unlink(copilot_hooks)
        print(f"[uninstall] removed {copilot_hooks}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[uninstall] could not remove {copilot_hooks}: {e}", file=sys.stderr)

    copilot_hooks_dir = os.path.dirname(copilot_hooks)
    try:
        os.rmdir(copilot_hooks_dir)
        print(f"[uninstall] removed empty {copilot_hooks_dir}")
    except OSError:
        pass

    try:
        os.unlink(POLICY_PATH)
        print(f"[uninstall] removed {POLICY_PATH}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[uninstall] could not remove {POLICY_PATH}: {e}",
              file=sys.stderr)
    try:
        os.rmdir(CODELIGHT_CONFIG_HOME)
    except OSError:
        pass

    for path in [SOCKET_PATH, MONITOR_STATE_DIR]:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            print(f"[uninstall] removed {path}")
        except FileNotFoundError:
            pass

    service_core.uninstall_service(run=subprocess.run)

    uninstall_vscode_extension()

    print("[uninstall] done")


# ── Systemd service install ───────────────────────────────────────────────────

def detect_installed_agents() -> set[str]:
    return vscode_core.detect_installed_agents(
        which=shutil.which, run=subprocess.run)


def _parse_agent_set(value: str | None) -> set[str]:
    return vscode_core.parse_agent_set(value, set(AGENT_REGISTRY))


def _find_vscode_cli() -> tuple[str, str] | None:
    return vscode_core.find_vscode_cli(which=shutil.which)


def _configure_vscode_settings(settings_path: str, secret: str, ws_port: int) -> None:
    vscode_core.configure_vscode_settings(settings_path, secret, ws_port)


def _find_local_vsix() -> str | None:
    return vscode_core.find_local_vsix(__file__)


def install_vscode_extension(secret: str = "", ws_port: int = 8765) -> None:
    vscode_core.install_vscode_extension(
        __file__, secret, ws_port, which=shutil.which, run=subprocess.run)


def uninstall_vscode_extension() -> None:
    vscode_core.uninstall_vscode_extension(
        which=shutil.which, run=subprocess.run)


def install_service(name: str, secret: str, ws_port: int, verbose: bool,
                    remote_control: bool = False,
                    permission_timeout: int = 60,
                    agents: set[str] | None = None,
                    github_org: str = "",
                    github_token_file: str = "") -> None:
    service_core.install_service(
        name=name,
        secret=secret,
        ws_port=ws_port,
        verbose=verbose,
        script_path=os.path.abspath(__file__),
        remote_control=remote_control,
        permission_timeout=permission_timeout,
        agents=agents,
        github_org=github_org,
        github_token_file=github_token_file,
        run=subprocess.run,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _verbose, _conversation_refresher

    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["dashboard"],
                        help="Run the terminal dashboard as a client.")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove codelight agent hooks and delete state files.")
    parser.add_argument("--install", action="store_true",
                        help="Install and start a systemd user service (requires --name).")
    parser.add_argument("--hook", metavar="STATE",
                        help="Hook mode: send STATE event to daemon and exit. "
                             "Used internally by agent hooks (working/waiting/ended).")
    parser.add_argument("--agent", default=DEFAULT_AGENT_ID,
                        help="Internal hook/runtime agent id (claude/copilot/codex).")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show low-level debug events (socket, API) in activity log")
    parser.add_argument("--ws-port", type=int, default=8765,
                        help="WebSocket port for clients (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="With 'dashboard': daemon host (default: 127.0.0.1)")
    parser.add_argument("--name", default=None,
                        help="mDNS service name visible to clients (required)")
    parser.add_argument("--secret", default="",
                        help="Shared secret for WebSocket auth (match in screen config)")
    parser.add_argument("--remote-control", action="store_true",
                        help="Let clients remotely approve agent permission prompts "
                             "and answer supported question prompts. "
                             "Requires --secret.")
    parser.add_argument("--permission-timeout", type=int, default=60,
                        help="Seconds to wait for a remote decision/answer before "
                             "falling back to the agent's built-in prompt (default: 60)")
    parser.add_argument("--agents", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--vscode", action="store_true",
                        help="With --install: also install the codelight VSCode "
                             "extension from the latest GitHub release")
    parser.add_argument("--github-org", default=os.environ.get("CODELIGHT_GITHUB_ORG", ""),
                        help="GitHub organization whose pooled Copilot AI-credit "
                             "usage should be shown")
    parser.add_argument("--github-token-file", default="",
                        help="File containing a GitHub token for Copilot billing. "
                             "Alternatively set CODELIGHT_GITHUB_TOKEN; if neither "
                             "is set, the gh credential is used when available.")
    args = parser.parse_args()

    if args.command == "dashboard":
        run_dashboard(args.host, args.ws_port, args.secret)
        return

    if args.uninstall:
        uninstall()
        return

    if args.install:
        if args.name is None:
            parser.error("--name is required with --install")
        if args.remote_control and not args.secret:
            parser.error("--remote-control requires --secret (remote approval/answers "
                         "are code-execution capability and must not be open to the LAN)")
        detected_agents = detect_installed_agents()
        print("[install] detected agents: "
              + (", ".join(sorted(detected_agents)) or "none"))
        install_service(args.name, args.secret, args.ws_port, args.verbose,
                        args.remote_control, args.permission_timeout,
                        detected_agents, args.github_org, args.github_token_file)
        if args.vscode:
            install_vscode_extension(args.secret, args.ws_port)
        return

    if args.hook == "permission":
        run_permission_hook(args.permission_timeout, agent_id=args.agent)
        return
    if args.hook == "permission-copilot":
        run_permission_hook(args.permission_timeout, copilot_mode=True,
                            agent_id=args.agent)
        return
    if args.hook == "permission-vscode":
        run_permission_hook(args.permission_timeout, vscode_prettool_mode=True,
                            agent_id=args.agent)
        return
    if args.hook == "question-vscode":
        run_question_hook(args.permission_timeout, vscode_prettool_mode=True,
                          agent_id=args.agent)
        return
    if args.hook == "question-codex":
        run_question_hook(args.permission_timeout, codex_context_mode=True,
                          agent_id=args.agent)
        return
    if args.hook == "question":
        run_question_hook(args.permission_timeout, agent_id=args.agent)
        return
    if args.hook:
        run_hook(args.hook, agent_id=args.agent)
        return

    if args.name is None:
        parser.error("--name is required (e.g. --name henrik-laptop). "
                     "It identifies this daemon to clients.")

    _verbose = args.verbose

    global _remote_permissions, _remote_questions, _permission_timeout
    global _github_org, _github_token_file
    _permission_timeout = args.permission_timeout
    _remote_permissions = args.remote_control
    _remote_questions   = args.remote_control
    _github_org = args.github_org
    _github_token_file = args.github_token_file
    if args.remote_control and not args.secret:
        print("[rc] --remote-control requires --secret — feature disabled",
              file=sys.stderr, flush=True)
        _remote_permissions = _remote_questions = False

    enabled_agents = (_parse_agent_set(args.agents)
                      if args.agents is not None else detect_installed_agents())
    print("[agents] enabled: " + (", ".join(sorted(enabled_agents)) or "none"),
          flush=True)

    if "claude" in enabled_agents:
        install_hooks(os.path.abspath(__file__), _remote_permissions,
                      _remote_questions, _permission_timeout)
    if "copilot" in enabled_agents:
        install_copilot_hooks(os.path.abspath(__file__), _remote_permissions,
                              _permission_timeout)
    if "codex" in enabled_agents:
        install_codex_hooks(os.path.abspath(__file__), _remote_permissions,
                            _remote_questions, _permission_timeout)

    print(f"codelight  [ws://0.0.0.0:{args.ws_port}]  (Ctrl-C to stop)", flush=True)

    _conversation_refresher = ConversationRefresher(
        active_path=_active_conversation_path,
        has_clients=_has_conversation_clients,
        broadcast=_broadcast_conversation,
        shutdown=_shutdown,
    )

    threading.Thread(target=_socket_thread, daemon=True).start()
    threading.Thread(target=_usage_thread,  daemon=True).start()
    threading.Thread(target=_conversation_refresh_thread, daemon=True).start()

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
