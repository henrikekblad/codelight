from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable


PENDING_COMPLETION_EVENTS = {
    "PermissionDenied",
    "Stop",
    "SessionEnd",
}


def should_cancel_pending_for_hook(state: str, hook_event: str) -> bool:
    """Whether a lifecycle event proves a local prompt is no longer pending."""
    event = str(hook_event or "").strip()
    if event:
        return event in PENDING_COMPLETION_EVENTS
    # Older codelight hooks did not forward their event name.
    return state in ("working", "ended")


def wait_remaining(entry: dict, *, now: float | None = None) -> float:
    return float(entry["expires"]) - (time.time() if now is None else now)


def question_wait_remaining(
    entry: dict,
    *,
    can_answer: bool,
    last_client_gone: float,
    reconnect_window: float,
    grace_deadline: float,
    now: float | None = None,
) -> float:
    now = time.time() if now is None else now
    if can_answer or (now - last_client_gone) < reconnect_window:
        return wait_remaining(entry, now=now)
    return grace_deadline - now


def wait_with_extend(entry: dict, *, tick: float = 5.0) -> None:
    """Block until the request is resolved or its extendable deadline passes."""
    while not entry["event"].is_set():
        remaining = wait_remaining(entry)
        if remaining <= 0:
            break
        entry["event"].wait(min(remaining, tick))


def wait_question(
    entry: dict,
    *,
    can_answer_questions,
    last_client_gone,
    no_client_grace: float,
    reconnect_window: float,
    tick: float = 2.0,
) -> None:
    grace_deadline = time.time() + no_client_grace
    while not entry["event"].is_set():
        remaining = question_wait_remaining(
            entry,
            can_answer=can_answer_questions(),
            last_client_gone=last_client_gone(),
            reconnect_window=reconnect_window,
            grace_deadline=grace_deadline,
        )
        if remaining <= 0:
            break
        entry["event"].wait(min(remaining, tick))


def _reply_and_close(conn, payload: dict) -> None:
    """Deliver a decision to a blocked hook over its held socket, then close."""
    try:
        conn.sendall((json.dumps(payload) + "\n").encode())
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def socket_responder(conn):
    """A responder that replies to a hook over its socket connection. Other
    transports (e.g. OpenCode's HTTP server) pass their own responder instead."""
    return lambda payload: _reply_and_close(conn, payload)


class SessionToolAllowances:
    """Tools the user allowed for the remainder of a session ("allow this
    session"). In-memory only — dies with the daemon; a session's entry is
    cleared when its SessionEnd hook arrives. Bounded so agents without a
    SessionEnd event can't grow it forever."""

    MAX_SESSIONS = 64

    def __init__(self):
        self._lock = threading.Lock()
        self._by_session: dict[str, set[str]] = {}

    def allow(self, session_id: str, tool_name: str) -> bool:
        sid = str(session_id or "").strip()
        tool = str(tool_name or "").strip()
        if not sid or sid == "unknown" or not tool or tool == "?":
            return False
        with self._lock:
            if sid not in self._by_session \
                    and len(self._by_session) >= self.MAX_SESSIONS:
                self._by_session.pop(next(iter(self._by_session)))
            self._by_session.setdefault(sid, set()).add(tool)
        return True

    def is_allowed(self, session_id: str, tool_name: str) -> bool:
        with self._lock:
            return str(tool_name or "") in self._by_session.get(
                str(session_id or ""), set())

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(str(session_id or ""), None)


class PendingRequests:
    """Thread-safe owner for remote permission/question request state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._permissions: dict[str, dict] = {}
        self._questions: dict[str, dict] = {}

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    @property
    def permissions(self) -> dict[str, dict]:
        return self._permissions

    @property
    def questions(self) -> dict[str, dict]:
        return self._questions

    def pending_session_ids(self) -> set[str]:
        with self._lock:
            return (
                {p["session_id"] for p in self._permissions.values()}
                | {q["session_id"] for q in self._questions.values()}
            )

    def add_permission(self, request_id: str, entry: dict) -> None:
        with self._lock:
            self._permissions[request_id] = entry

    def add_question(self, request_id: str, entry: dict) -> None:
        with self._lock:
            self._questions[request_id] = entry

    def pop_permission(self, request_id: str) -> dict | None:
        with self._lock:
            return self._permissions.pop(request_id, None)

    def pop_question(self, request_id: str) -> dict | None:
        with self._lock:
            return self._questions.pop(request_id, None)

    def pending_payloads(
        self,
        permission_payload: Callable[[dict], dict],
        question_payload: Callable[[dict], dict],
    ) -> list[dict]:
        with self._lock:
            return (
                [permission_payload(entry) for entry in self._permissions.values()]
                + [question_payload(entry) for entry in self._questions.values()]
            )

    def permission_persistence_request(
        self,
        request_id: str,
    ) -> tuple[str, str, str, str] | None:
        with self._lock:
            entry = self._permissions.get(request_id)
            if entry is None or entry["decision"] is not None or entry["by"] is not None:
                return None
            return (str(entry.get("cwd") or ""),
                    str(entry.get("policy_command") or ""),
                    str(entry.get("tool_name") or ""),
                    str(entry.get("session_id") or ""))

    def finish_permission_persistence(
        self,
        request_id: str,
        *,
        by: str,
        kind: str,
        persisted: bool,
        value: str,
    ) -> bool:
        with self._lock:
            entry = self._permissions.get(request_id)
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

    def resolve_permission(self, request_id: str, decision: str, by: str) -> bool:
        with self._lock:
            entry = self._permissions.get(request_id)
            if entry is None or entry["decision"] is not None or entry["by"] is not None:
                return False
            entry["decision"] = None if decision == "skip" else decision
            entry["by"] = by
            entry["event"].set()
            return True

    def resolve_question(self, request_id: str, answers, by: str) -> bool:
        with self._lock:
            entry = self._questions.get(request_id)
            if entry is None or entry["by"] is not None:
                return False
            entry["by"] = by
            if isinstance(answers, dict) and answers:
                entry["answers"] = answers
            entry["event"].set()
            return True

    def extend(self, request_id: str, timeout_secs: int) -> bool:
        with self._lock:
            entry = self._permissions.get(request_id) or self._questions.get(request_id)
            if entry is None:
                return False
            entry["expires"] = time.time() + timeout_secs
            return True

    def cancel_for_session(self, session_id: str) -> list[dict]:
        with self._lock:
            permissions = [
                entry for entry in self._permissions.values()
                if entry["session_id"] == session_id and entry["decision"] is None
            ]
            for entry in permissions:
                entry["by"] = "cancelled"

            questions = [
                entry for entry in self._questions.values()
                if entry["session_id"] == session_id and entry["by"] is None
            ]
            for entry in questions:
                entry["by"] = "cancelled"

        for entry in permissions + questions:
            entry["event"].set()
        return permissions + questions


class RemoteRequestManager:
    """Coordinates remote permission/question request lifecycle."""

    def __init__(
        self,
        *,
        pending: PendingRequests,
        permission_timeout: Callable[[], int],
        remote_permissions: Callable[[], bool],
        remote_questions: Callable[[], bool],
        normalize_agent_id: Callable[[str | None], str],
        permission_payload: Callable[[dict], dict],
        question_payload: Callable[[dict], dict],
        permission_resolved_payload: Callable[[dict, str, str, dict | None], dict],
        question_resolved_payload: Callable[[dict, str], dict],
        broadcast_remote: Callable[[dict, str], None],
        update_session: Callable[[str, str, str], None],
        push_status: Callable[[], None],
        log: Callable[[str], None],
        allow_folder: Callable[[str], tuple[bool, str]],
        allow_command: Callable[[str, str], tuple[bool, str]],
        allow_tool: Callable[[str], tuple[bool, str]],
        can_answer_questions: Callable[[], bool],
        last_question_client_gone: Callable[[], float],
        no_client_grace: float,
        reconnect_window: float,
    ) -> None:
        self.pending = pending
        self.permission_timeout = permission_timeout
        self.remote_permissions = remote_permissions
        self.remote_questions = remote_questions
        self.normalize_agent_id = normalize_agent_id
        self.permission_payload = permission_payload
        self.question_payload = question_payload
        self.permission_resolved_payload = permission_resolved_payload
        self.question_resolved_payload = question_resolved_payload
        self.broadcast_remote = broadcast_remote
        self.update_session = update_session
        self.push_status = push_status
        self.log = log
        self.allow_folder = allow_folder
        self.allow_command = allow_command
        self.allow_tool = allow_tool
        self.session_allowances = SessionToolAllowances()
        self.can_answer_questions = can_answer_questions
        self.last_question_client_gone = last_question_client_gone
        self.no_client_grace = no_client_grace
        self.reconnect_window = reconnect_window

    def pending_payloads(self) -> list[dict]:
        return self.pending.pending_payloads(
            self.permission_payload,
            self.question_payload,
        )

    def resolve_permission(self, request_id: str, decision: str, by: str) -> bool:
        if decision not in ("allow", "deny", "skip", "allow_folder",
                            "allow_command", "allow_tool", "allow_tool_session"):
            return False

        if decision in ("allow_folder", "allow_command",
                        "allow_tool", "allow_tool_session"):
            pending = self.pending.permission_persistence_request(request_id)
            if pending is None:
                return False
            cwd, policy_command, tool_name, session_id = pending

            if decision == "allow_folder":
                persisted, value = self.allow_folder(cwd)
                kind = "folder"
            elif decision == "allow_command":
                persisted, value = self.allow_command(policy_command, cwd)
                kind = "command"
            elif decision == "allow_tool":
                persisted, value = self.allow_tool(tool_name)
                kind = "tool"
            else:
                persisted = self.session_allowances.allow(session_id, tool_name)
                value = tool_name
                kind = "tool_session"

            return self.pending.finish_permission_persistence(
                request_id,
                by=by,
                kind=kind,
                persisted=persisted,
                value=value,
            )

        return self.pending.resolve_permission(request_id, decision, by)

    def resolve_question(self, request_id: str, answers, by: str) -> bool:
        return self.pending.resolve_question(request_id, answers, by)

    def extend(self, request_id: str) -> bool:
        return self.pending.extend(request_id, self.permission_timeout())

    def cancel_for_session(self, session_id: str) -> None:
        self.pending.cancel_for_session(session_id)

    def clear_session_allowances(self, session_id: str) -> None:
        self.session_allowances.clear(session_id)

    def register_permission(self, conn, msg: dict, *, responder=None) -> None:
        # Hooks reply over their held socket; other transports (OpenCode's HTTP
        # server) pass an explicit responder instead of a conn.
        if responder is None:
            responder = socket_responder(conn)
        if not self.remote_permissions():
            responder({"decision": None})
            return

        request_id = str(msg.get("prompt_id") or "") or uuid.uuid4().hex
        session_id = msg.get("session_id", "unknown")

        # "Allow this tool for the session" — answer without prompting anyone.
        tool_name = str(msg.get("tool_name") or "")
        if self.session_allowances.is_allowed(session_id, tool_name):
            responder({"decision": "allow"})
            self.log(f"[perm] {tool_name} → allow (session allowance)")
            return
        entry = {
            "responder":  responder,
            "id":         request_id,
            "session_id": session_id,
            "agent_id":   self.normalize_agent_id(msg.get("agent_id")),
            "tool_name":  msg.get("tool_name", "?"),
            "summary":    msg.get("summary", "") or msg.get("tool_name", "?"),
            "tool_input": msg.get("tool_input", {}),
            "policy_command": msg.get("policy_command", ""),
            "cwd":        msg.get("cwd", ""),
            "event":      threading.Event(),
            "decision":   None,
            "by":         None,
            "expires":    time.time() + self.permission_timeout(),
        }
        self.pending.add_permission(request_id, entry)
        self.update_session(session_id, "waiting", entry["agent_id"])
        self.log(f"[perm] request: {entry['summary'][:60]}")
        self.push_status()
        self.broadcast_remote(self.permission_payload(entry), "PermissionRequest")
        threading.Thread(
            target=self._permission_waiter, args=(entry,), daemon=True).start()

    def register_question(self, conn, msg: dict, *, responder=None) -> None:
        if responder is None:
            responder = socket_responder(conn)
        if not self.remote_questions():
            responder({"answers": None})
            return

        request_id = str(msg.get("prompt_id") or "") or uuid.uuid4().hex
        session_id = msg.get("session_id", "unknown")
        entry = {
            "responder":  responder,
            "id":         request_id,
            "session_id": session_id,
            "agent_id":   self.normalize_agent_id(msg.get("agent_id")),
            "questions":  msg.get("questions", []),
            "cwd":        msg.get("cwd", ""),
            "event":      threading.Event(),
            "answers":    None,
            "by":         None,
            "expires":    time.time() + self.permission_timeout(),
        }
        self.pending.add_question(request_id, entry)
        self.update_session(session_id, "waiting", entry["agent_id"])
        self.log(f"[question] request: {len(entry['questions'])} question(s)")
        self.push_status()
        self.broadcast_remote(self.question_payload(entry), "QuestionRequest")
        threading.Thread(
            target=self._question_waiter, args=(entry,), daemon=True).start()

    def _permission_waiter(self, entry: dict) -> None:
        wait_with_extend(entry)
        self.pending.pop_permission(entry["id"])
        decision = entry["decision"]
        by = entry["by"]
        persistence = entry.get("persistence") \
            if isinstance(entry.get("persistence"), dict) else None

        entry["responder"]({"decision": decision, "persistence": persistence})

        outcome = decision or (
            "cancelled" if by == "cancelled" else "skip" if by else "timeout")
        if decision == "allow" and persistence and persistence.get("requested"):
            outcome = f"allow_{persistence.get('kind', 'once')}"
        self.log(f"[perm] {entry['summary'][:60]} → {outcome}"
                 + (f" (by {by})" if decision else ""))
        self.broadcast_remote(
            self.permission_resolved_payload(entry, outcome, by or "", persistence),
            "PermissionResolved",
        )
        self.push_status()

    def _question_waiter(self, entry: dict) -> None:
        wait_question(
            entry,
            can_answer_questions=self.can_answer_questions,
            last_client_gone=self.last_question_client_gone,
            no_client_grace=self.no_client_grace,
            reconnect_window=self.reconnect_window,
        )
        self.pending.pop_question(entry["id"])
        answers = entry["answers"]
        by = entry["by"]

        entry["responder"]({"answers": answers})

        outcome = "answered" if answers else (
            "cancelled" if by == "cancelled" else "timeout")
        self.log(f"[question] {entry['id'][:8]}… → {outcome}"
                 + (f" (by {by})" if answers else ""))
        self.broadcast_remote(
            self.question_resolved_payload(entry, by or ""),
            "QuestionResolved",
        )
        self.push_status()
