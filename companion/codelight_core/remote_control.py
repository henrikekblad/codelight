from __future__ import annotations

import time


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
