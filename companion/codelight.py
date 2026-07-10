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
import signal
import sys
import threading
import time
from datetime import datetime
from codelight_core.agents.registry import AgentRegistry
from codelight_core import auth as auth_core
from codelight_core import conversation as conversation_core
from codelight_core.conversation import ConversationRefresher
from codelight_core import dashboard_client
from codelight_core import discovery as discovery_core
from codelight_core import hook_commands
from codelight_core import lifecycle
from codelight_core import policy as policy_core
from codelight_core import remote_control
from codelight_core import remote_payloads
from codelight_core import socket_server
from codelight_core.state import CodelightState
from codelight_core import transcript as transcript_core
from codelight_core.usage import UsagePoller
from codelight_core.ws_server import CodelightWebsocketHub

try:
    import websockets as _websockets
    _have_websockets = True
except ImportError:
    _have_websockets = False

# ── Config ────────────────────────────────────────────────────────────────────

CODELIGHT_CONFIG_HOME = os.path.expanduser(
    os.environ.get("CODELIGHT_CONFIG_HOME", "~/.config/codelight"))
MONITOR_STATE_DIR = os.path.join(CODELIGHT_CONFIG_HOME, "monitor_state")
SOCKET_PATH       = os.path.join(CODELIGHT_CONFIG_HOME, "codelight.sock")
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

_policy_lock: threading.Lock = threading.Lock()
# session_id → {"state": "working"|"waiting", "time": float}

_ws_hub: CodelightWebsocketHub | None = None

# Remote control (armed via --remote-control, requires --secret):
# approve tool permissions AND answer AskUserQuestion prompts remotely.
_remote_permissions: bool = False
_remote_questions:   bool = False
_permission_timeout: int  = 60
_pending_requests = remote_control.PendingRequests()
_lock = _pending_requests.lock
_pending_perms = _pending_requests.permissions
_pending_questions = _pending_requests.questions
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
_remote_manager: remote_control.RemoteRequestManager | None = None

def _load_config() -> dict:
    """~/.config/codelight/config.json — see companion/AGENTS.md for keys."""
    try:
        with open(os.path.join(CODELIGHT_CONFIG_HOME, "config.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[config] could not read config.json: {e}",
              file=sys.stderr, flush=True)
        return {}


_config = _load_config()


def _new_agent_registry(log=None) -> AgentRegistry:
    agents_config = _config.get("agents")
    return AgentRegistry(
        agents_config=agents_config if isinstance(agents_config, dict) else {},
        log=log,
    )


_agents = _new_agent_registry()
AGENT_REGISTRY = _agents.display_registry()
DEFAULT_AGENT_ID = _agents.default_agent_id
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


def _broadcast(payload: dict) -> None:
    """Thread-safe push to all WebSocket clients and the D-Bus signal."""
    if _ws_hub is not None:
        _ws_hub.broadcast_status(payload)


# ── Session state ─────────────────────────────────────────────────────────────

def _update_session(session_id: str, state: str,
                    transcript: str = "", cwd: str = "",
                    agent_id: str = DEFAULT_AGENT_ID) -> None:
    normalized_agent = _normalize_agent_id(agent_id)
    if not transcript:
        transcript = _agents.transcript_path_for_session(
            normalized_agent, session_id)
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
    for agent_id, path in _agents.latest_transcript_fallbacks():
        if path:
            return (agent_id, path)
    return ("", "")


def _client_config(client: str = "") -> dict:
    """One-time per-connection client config: agent branding and defaults.

    ``client`` is the requesting client's self-reported type (vscode/android/
    gnome/screen/…); the screen gets bitmap logos instead of SVGs.
    """
    return {
        "default_agent_id": DEFAULT_AGENT_ID,
        "agents": _agents.client_metadata(client),
    }


def _parse_transcript(path: str, max_msgs: int = 60) -> list[dict]:
    return transcript_core.parse_transcript(
        path, tool_summary=_tool_summary,
        extractors=_agents.transcript_extractors(), max_msgs=max_msgs)


def _conversation_payload() -> dict | None:
    """Build the {"type":"conversation", ...} feed for the active session."""
    return conversation_core.build_payload(
        active_transcript=_active_transcript,
        parse_transcript=_parse_transcript,
        conversation_agent=_state.conversation_agent,
        agent_display_name=_agent_display_name,
    )


def _active_conversation_path() -> str:
    return _active_transcript()[1]


def _has_conversation_clients() -> bool:
    return _ws_hub.has_conversation_clients() if _ws_hub is not None else False


def _conversation_refresh_thread() -> None:
    if _conversation_refresher is not None:
        _conversation_refresher.run()


def _notify_conversation_changed() -> None:
    if _conversation_refresher is not None:
        _conversation_refresher.notify()


def _broadcast_conversation() -> None:
    """Push the conversation feed to subscribed clients (thread-safe)."""
    if _ws_hub is not None:
        _ws_hub.broadcast_conversation()


def _overall_status() -> tuple[int, str, dict[str, str], str]:
    """Return (active_count, overall_status) from in-memory session state.
    Cleans up sessions that have been silent longer than IDLE_WINDOW."""
    # Sessions with a pending remote permission/question request stay alive —
    # the 30 s waiting window would otherwise drop them mid-request.
    return _state.overall_status(_pending_requests.pending_session_ids())


def _status_snapshot() -> dict:
    payload = _state.status_snapshot(_pending_requests.pending_session_ids())
    payload["activity"] = list(_log_lines)
    payload["clients"] = {
        "websocket": _ws_hub.client_count() if _ws_hub is not None else 0,
        "dbus": _ws_hub.dbus_exported() if _ws_hub is not None else False,
    }
    return payload


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
    if _ws_hub is not None:
        _ws_hub.broadcast_remote(payload, dbus_signal)


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
    return _remote_request_manager().resolve_permission(request_id, decision, by)


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
    return _remote_request_manager().extend(request_id)


def _cancel_permissions_for(session_id: str) -> None:
    """Session activity/end — wake up its pending permission AND question
    requests without a decision (answered locally)."""
    _remote_request_manager().cancel_for_session(session_id)


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
    _remote_request_manager()._permission_waiter(entry)


def _register_permission(conn, msg: dict) -> None:
    """Take ownership of the hook's socket connection and start the approval
    round-trip. Called from the socket thread; must not block it."""
    _remote_request_manager().register_permission(conn, msg)


# ── Remote question answering via PreToolUse ──────────────────────────────────

def _question_request_payload(entry: dict) -> dict:
    return remote_payloads.question_request_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        agent_display_name=_agent_display_name,
    )


def _permission_resolved_payload(entry: dict, decision: str, by: str,
                                 persistence: dict | None) -> dict:
    return remote_payloads.permission_resolved_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        decision=decision,
        by=by,
        persistence=persistence,
        agent_display_name=_agent_display_name,
    )


def _question_resolved_payload(entry: dict, by: str) -> dict:
    return remote_payloads.question_resolved_payload(
        {**entry, "agent_id": _normalize_agent_id(entry.get("agent_id"))},
        by=by,
        agent_display_name=_agent_display_name,
    )


def _remote_request_manager() -> remote_control.RemoteRequestManager:
    global _remote_manager
    if _remote_manager is None:
        _remote_manager = remote_control.RemoteRequestManager(
            pending=_pending_requests,
            permission_timeout=lambda: _permission_timeout,
            remote_permissions=lambda: _remote_permissions,
            remote_questions=lambda: _remote_questions,
            normalize_agent_id=_normalize_agent_id,
            permission_payload=_perm_request_payload,
            question_payload=_question_request_payload,
            permission_resolved_payload=_permission_resolved_payload,
            question_resolved_payload=_question_resolved_payload,
            broadcast_remote=_broadcast_rc,
            update_session=lambda session_id, state, agent_id: _update_session(
                session_id, state, agent_id=agent_id),
            push_status=_push,
            log=_log,
            allow_folder=_allow_folder,
            allow_command=_allow_command,
            can_answer_questions=_can_answer_questions,
            last_question_client_gone=lambda: _last_qclient_gone,
            no_client_grace=NO_CLIENT_GRACE,
            reconnect_window=RECONNECT_WINDOW,
        )
    return _remote_manager


def _pending_remote_payloads() -> list[dict]:
    return _remote_request_manager().pending_payloads()


def _resolve_question(request_id: str, answers, by: str) -> bool:
    """Resolve a pending question. First response wins. A non-empty dict of
    {question: answer_string} answers it; an empty/None answers is an explicit
    skip (reply null → hook falls through to Claude's dialog immediately)."""
    return _remote_request_manager().resolve_question(request_id, answers, by)


def _question_waiter(entry: dict) -> None:
    """Per-request thread: wait for answers (or timeout), reply to the blocked
    hook, and notify clients. Reply {"answers": {...}} → hook emits updatedInput;
    {"answers": null} → hook prints nothing → Claude's local dialog."""
    _remote_request_manager()._question_waiter(entry)


def _gnome_present(feature: str) -> bool:
    """True if a GNOME extension announced it can answer `feature` recently."""
    return (time.time() - _gnome_last_seen < GNOME_PRESENCE_TTL
            and feature in _gnome_features)


def _announce_gnome(features: list[str]) -> bool:
    """Record a GNOME extension feature heartbeat received over D-Bus."""
    global _gnome_last_seen, _gnome_features
    _gnome_last_seen = time.time()
    _gnome_features = set(features)
    return True


def _can_answer_questions() -> bool:
    """True if any client (WS or GNOME) is currently able to answer questions."""
    ws_can_answer = _ws_hub.has_question_clients() if _ws_hub is not None else False
    return ws_can_answer or _gnome_present("questions")


def _note_qclient_gone() -> None:
    """Record that a question-answering WS client just disconnected, so a brief
    reconnect (VSCode restart) isn't mistaken for an unattended session."""
    global _last_qclient_gone
    _last_qclient_gone = time.time()


def _register_question(conn, msg: dict) -> None:
    """Take ownership of the hook's socket connection and start the answer
    round-trip for an AskUserQuestion PreToolUse hook."""
    _remote_request_manager().register_question(conn, msg)


# ── Permission policy compatibility helpers ──────────────────────────────────

def _is_trusted_repo_cwd(cwd: str) -> bool:
    return policy_core.is_trusted_repo_cwd(POLICY_PATH, cwd)


def _allow_folder(cwd: str) -> tuple[bool, str]:
    return policy_core.allow_folder(POLICY_PATH, _policy_lock, cwd)


def _is_allowed_command(tool_name: str, tool_input, cwd: str) -> bool:
    return policy_core.is_allowed_command(POLICY_PATH, tool_name, tool_input, cwd)


def _allow_command(command: str, cwd: str) -> tuple[bool, str]:
    return policy_core.allow_command(POLICY_PATH, _policy_lock, command, cwd)


def _tool_summary(tool_name: str, tool_input: dict) -> str:
    return policy_core.tool_summary(tool_name, tool_input)


def _is_safe_trusted_apply_patch(tool_name: str, tool_input, cwd: str) -> bool:
    return policy_core.is_safe_trusted_apply_patch(
        POLICY_PATH, tool_name, tool_input, cwd)


# ── Hook mode ─────────────────────────────────────────────────────────────────

def run_hook(state: str, agent_id: str = DEFAULT_AGENT_ID) -> None:
    hook_commands.run_status_hook(
        state,
        agent_id=agent_id,
        socket_path=SOCKET_PATH,
        monitor_state_dir=MONITOR_STATE_DIR,
        normalize_agent_id=_normalize_agent_id,
    )


def run_permission_hook(wait_secs: int, mode, agent_id: str | None = None) -> None:
    hook_commands.run_permission_hook(
        mode=mode,
        agent_id=agent_id,
        auto_allow_tools=_agents.trusted_auto_allow_tools,
        socket_path=SOCKET_PATH,
        monitor_state_dir=MONITOR_STATE_DIR,
        policy_path=POLICY_PATH,
        policy_lock=_policy_lock,
        hook_wait_ceiling=HOOK_WAIT_CEILING,
        normalize_agent_id=_normalize_agent_id,
        agent_display_name=_agent_display_name,
    )


def run_question_hook(wait_secs: int, mode, agent_id: str | None = None) -> None:
    hook_commands.run_question_hook(
        mode=mode,
        agent_id=agent_id,
        socket_path=SOCKET_PATH,
        hook_wait_ceiling=HOOK_WAIT_CEILING,
        normalize_agent_id=_normalize_agent_id,
        agent_display_name=_agent_display_name,
    )

# ── Usage API ─────────────────────────────────────────────────────────────────
# Credentials are read fresh each poll so token rotations are picked up automatically.

def _usage_fetchers():
    return _new_agent_registry(log=vprint).usage_fetchers()


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
    global _ws_hub

    if not _have_websockets:
        print("[ws] websockets not installed — clients unavailable", file=sys.stderr)
        print("[ws] Install: pip install websockets", file=sys.stderr)
        return

    _ws_hub = CodelightWebsocketHub(
        websockets_module=_websockets,
        shutdown=_shutdown,
        remote_permissions=lambda: _remote_permissions,
        remote_questions=lambda: _remote_questions,
        client_config=_client_config,
        status_snapshot=_status_snapshot,
        overall_status=_overall_status,
        pending_payloads=_pending_remote_payloads,
        conversation_payload=_conversation_payload,
        notify_conversation_changed=_notify_conversation_changed,
        note_question_client_gone=_note_qclient_gone,
        respond_permission=_resolve_permission,
        respond_question=_resolve_question,
        extend_request=_extend_request,
        announce_gnome=_announce_gnome,
        log=_log,
        verbose_log=vprint,
    )
    try:
        _ws_hub.run(port=port, secret=secret)
    finally:
        _ws_hub = None

def _mdns_thread(port: int, name: str) -> None:
    discovery_core.advertise_mdns(
        port=port,
        name=name,
        shutdown=_shutdown,
        log=_log,
        verbose_log=vprint,
    )


def _handle_socket_message(conn, msg: dict) -> bool:
    """Handle one parsed Unix-socket hook message.

    Returns True when the handler takes ownership of the connection.
    """
    if msg.get("type") == "permission_request":
        _register_permission(conn, msg)
        return True
    if msg.get("type") == "question_request":
        _register_question(conn, msg)
        return True

    sid = msg.get("session_id", "unknown")
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
    return False


def _socket_thread() -> None:
    """Accept hook events on the Unix socket and broadcast to clients immediately."""
    socket_server.serve_hook_socket(
        socket_path=SOCKET_PATH,
        shutdown=_shutdown,
        handle_message=_handle_socket_message,
        log=vprint,
    )


def _usage_thread() -> None:
    """Refresh supported-agent usage and broadcast after each update."""
    UsagePoller(
        state=_state,
        fetchers=_usage_fetchers(),
        interval=USAGE_INTERVAL,
        shutdown=_shutdown,
        log=_log,
        push=_push,
    ).run()

# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall() -> None:
    """Remove all codelight hooks, socket file, and state directory."""
    lifecycle.uninstall(
        agent_registry=_new_agent_registry(),
        policy_path=POLICY_PATH,
        config_home=CODELIGHT_CONFIG_HOME,
        socket_path=SOCKET_PATH,
        monitor_state_dir=MONITOR_STATE_DIR,
    )


def _parse_agent_set(value: str | None) -> set[str]:
    return lifecycle.parse_agent_set(value, set(AGENT_REGISTRY))


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
                        help="Internal hook/runtime agent id ("
                             + "/".join(AGENT_REGISTRY) + ").")
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
        detected_agents = lifecycle.detect_installed_agents(_new_agent_registry())
        print("[install] detected agents: "
              + (", ".join(sorted(detected_agents)) or "none"))
        lifecycle.install_service(
            script_path=os.path.abspath(__file__),
            name=args.name,
            secret=args.secret,
            ws_port=args.ws_port,
            verbose=args.verbose,
            remote_control=args.remote_control,
            permission_timeout=args.permission_timeout,
            agents=detected_agents,
        )
        if args.vscode:
            lifecycle.install_vscode_extension(
                os.path.abspath(__file__), args.secret, args.ws_port)
        return

    if args.hook:
        hook_mode = _agents.hook_modes().get(args.hook)
        if hook_mode is None:
            run_hook(args.hook, agent_id=args.agent)
        elif hook_mode.kind == "permission":
            run_permission_hook(args.permission_timeout, hook_mode,
                                agent_id=args.agent)
        else:
            run_question_hook(args.permission_timeout, hook_mode,
                              agent_id=args.agent)
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

    enabled_agents = (_parse_agent_set(args.agents)
                      if args.agents is not None
                      else lifecycle.detect_installed_agents(_new_agent_registry()))
    print("[agents] enabled: " + (", ".join(sorted(enabled_agents)) or "none"),
          flush=True)

    lifecycle.install_agent_hooks(
        agent_registry=_new_agent_registry(log=vprint),
        enabled_agents=enabled_agents,
        script_path=os.path.abspath(__file__),
        hook_wait_ceiling=HOOK_WAIT_CEILING,
        remote_permissions=_remote_permissions,
        remote_questions=_remote_questions,
        permission_timeout=_permission_timeout,
        log=vprint,
    )

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
