from __future__ import annotations

import threading
import time
from collections.abc import Callable


PENDING_COMPLETION_EVENTS = {
    "PostToolUse",
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
    ) -> tuple[str, str] | None:
        with self._lock:
            entry = self._permissions.get(request_id)
            if entry is None or entry["decision"] is not None or entry["by"] is not None:
                return None
            return (str(entry.get("cwd") or ""),
                    str(entry.get("policy_command") or ""))

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
