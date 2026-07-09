from __future__ import annotations

import json
import os

from codelight_core.timefmt import format_epoch_countdown


class CodexAgent:
    def __init__(self, code_home: str) -> None:
        self.code_home = code_home

    def rollout_path_for_session(self, session_id: str) -> str:
        return rollout_path_for_session(self.code_home, session_id)

    def latest_rollout_path(self) -> str:
        return latest_rollout_path(self.code_home)

    def usage_from_rollout(self, path: str) -> dict | None:
        return usage_from_rollout(path)

    def get_usage(self) -> dict | None:
        return get_usage(self.code_home)


def rollout_path_for_session(code_home: str, session_id: str) -> str:
    """Find Codex's rollout JSONL for a hook session/thread id."""
    sid = str(session_id or "").strip()
    if not sid or sid == "unknown":
        return ""
    try:
        base = os.path.realpath(os.path.join(code_home, "sessions"))
        suffix = f"-{sid}.jsonl"
        for root, _, files in os.walk(base):
            for name in files:
                if name.endswith(suffix):
                    candidate = os.path.realpath(os.path.join(root, name))
                    if candidate.startswith(base + os.sep):
                        return candidate
    except Exception:
        pass
    return ""


def latest_rollout_path(code_home: str) -> str:
    try:
        base = os.path.join(code_home, "sessions")
        newest_path = ""
        newest_mtime = 0.0
        for root, _, files in os.walk(base):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest_path = path
        return newest_path
    except Exception:
        return ""


def usage_from_rollout(path: str) -> dict | None:
    """Read the newest Codex 5-hour and weekly rate-limit snapshot."""
    if not path:
        return None
    try:
        with open(path, "r") as stream:
            lines = stream.readlines()
    except Exception:
        return None

    limits = None
    for raw in reversed(lines):
        try:
            record = json.loads(raw)
        except Exception:
            continue
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        candidate = payload.get("rate_limits")
        if isinstance(candidate, dict) and candidate.get("limit_id") == "codex":
            limits = candidate
            break
    if not limits:
        return None

    primary = limits.get("primary") if isinstance(limits.get("primary"), dict) else {}
    secondary = limits.get("secondary") if isinstance(limits.get("secondary"), dict) else {}

    def pct(window: dict) -> float:
        try:
            return max(0.0, min(1.0, float(window.get("used_percent") or 0.0) / 100.0))
        except (TypeError, ValueError):
            return 0.0

    def reset_at(window: dict) -> int:
        try:
            return int(window.get("resets_at") or 0)
        except (TypeError, ValueError):
            return 0

    session_reset_at = reset_at(primary)
    weekly_reset_at = reset_at(secondary)
    return {
        "session_pct": pct(primary),
        "weekly_pct": pct(secondary),
        "session_reset": format_epoch_countdown(session_reset_at),
        "weekly_reset": format_epoch_countdown(weekly_reset_at),
        "session_reset_at": session_reset_at,
        "weekly_reset_at": weekly_reset_at,
    }


def get_usage(code_home: str) -> dict | None:
    return usage_from_rollout(latest_rollout_path(code_home))
