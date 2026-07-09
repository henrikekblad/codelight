from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_USAGE: dict[str, Any] = {
    "session_pct": 0.0,
    "weekly_pct": 0.0,
    "session_reset": "--",
    "weekly_reset": "--",
    "session_reset_at": 0,
    "weekly_reset_at": 0,
}


@dataclass(frozen=True)
class ActiveTranscript:
    session_id: str
    path: str


class CodelightState:
    """Thread-safe owner for live session state and per-agent usage caches.

    The daemon still exposes legacy helper functions, but they should take
    snapshots from this object instead of reaching into module globals. That
    keeps broadcaster/rendering code on immutable copies and makes later agent
    backends additive.
    """

    def __init__(
        self,
        *,
        default_agent_id: str,
        agent_registry: dict[str, dict[str, str]],
        idle_window: int,
        idle_window_waiting: int,
    ) -> None:
        self._lock = threading.RLock()
        self._default_agent_id = default_agent_id
        self._agent_registry = agent_registry
        self._idle_window = idle_window
        self._idle_window_waiting = idle_window_waiting
        self._sessions: dict[str, dict[str, Any]] = {}
        self._usage_cache: dict[str, Any] = dict(DEFAULT_USAGE)
        self._codex_usage_cache: dict[str, Any] = {}
        self._copilot_usage_cache: dict[str, Any] = {}
        self._last_transcript: dict[str, str] = {"sid": "", "path": ""}
        self._last_active_agent: str = default_agent_id

    def normalize_agent_id(self, agent_id: str | None) -> str:
        aid = str(agent_id or "").strip().lower()
        return aid if aid else self._default_agent_id

    def agent_display_name(self, agent_id: str | None) -> str:
        aid = self.normalize_agent_id(agent_id)
        if aid in self._agent_registry:
            return self._agent_registry[aid]["display"]
        return aid.capitalize() if aid else self._agent_registry[self._default_agent_id]["display"]

    def agent_meter_titles(self, agent_id: str | None) -> tuple[str, str]:
        display = self.agent_display_name(agent_id)
        return f"{display} Weekly", f"{display} Session"

    def update_session(
        self,
        session_id: str,
        state: str,
        *,
        transcript: str = "",
        cwd: str = "",
        agent_id: str | None = None,
    ) -> None:
        normalized_agent = self.normalize_agent_id(agent_id)
        with self._lock:
            if transcript:
                self._last_transcript = {
                    "sid": session_id,
                    "path": transcript,
                    "agent_id": normalized_agent,
                }
            if state == "ended":
                self._sessions.pop(session_id, None)
            else:
                info = dict(self._sessions.get(session_id, {}))
                info["state"] = state
                info["time"] = time.time()
                if transcript:
                    info["transcript"] = transcript
                if cwd:
                    info["cwd"] = cwd
                info["agent_id"] = normalized_agent
                self._sessions[session_id] = info
            if state in ("working", "waiting"):
                self._last_active_agent = normalized_agent

    def active_transcript(self) -> ActiveTranscript:
        with self._lock:
            best: tuple[str, float, str] | None = None
            for sid, info in self._sessions.items():
                transcript = str(info.get("transcript") or "")
                if transcript and (best is None or float(info["time"]) > best[1]):
                    best = (sid, float(info["time"]), transcript)
            if best:
                return ActiveTranscript(best[0], best[2])
            path = self._last_transcript.get("path", "")
            if path:
                return ActiveTranscript(self._last_transcript.get("sid", ""), path)
        return ActiveTranscript("", "")

    def conversation_agent(self, session_id: str) -> str:
        with self._lock:
            info = self._sessions.get(session_id, {})
            return self.normalize_agent_id(
                info.get("agent_id")
                or self._last_transcript.get("agent_id")
                or self._last_active_agent
            )

    @staticmethod
    def _status_rank(status: str) -> int:
        return {"idle": 0, "waiting": 1, "working": 2}.get(status, 0)

    def overall_status(self, pending_session_ids: set[str] | None = None) -> tuple[int, str, dict[str, str], str]:
        pending_session_ids = pending_session_ids or set()
        now = time.time()
        active = 0
        overall = "idle"
        per_agent: dict[str, str] = {}
        with self._lock:
            last_agent = self.normalize_agent_id(self._last_active_agent)
            stale = [
                sid for sid, info in self._sessions.items()
                if sid not in pending_session_ids
                and now - float(info["time"]) > (
                    self._idle_window_waiting
                    if info.get("state") == "waiting"
                    else self._idle_window
                )
            ]
            for sid in stale:
                del self._sessions[sid]
            for info in self._sessions.values():
                active += 1
                state = str(info.get("state") or "idle")
                agent_id = self.normalize_agent_id(info.get("agent_id"))
                prev = per_agent.get(agent_id, "idle")
                if self._status_rank(state) > self._status_rank(prev):
                    per_agent[agent_id] = state
                if state == "working":
                    overall = "working"
                elif state == "waiting" and overall != "working":
                    overall = "waiting"
            if not per_agent:
                per_agent[last_agent] = "idle"
            return active, overall, per_agent, last_agent

    def update_usage(
        self,
        *,
        claude: dict[str, Any] | None = None,
        codex: dict[str, Any] | None = None,
        copilot: dict[str, Any] | None = None,
        clear_codex: bool = False,
        clear_copilot: bool = False,
    ) -> None:
        with self._lock:
            if claude is not None:
                self._usage_cache.update(claude)
            if codex is not None:
                self._codex_usage_cache.update(codex)
            elif clear_codex:
                self._codex_usage_cache.clear()
            if copilot is not None:
                self._copilot_usage_cache.update(copilot)
            elif clear_copilot:
                self._copilot_usage_cache.clear()

    def usage_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._usage_cache)

    def status_snapshot(self, pending_session_ids: set[str] | None = None) -> dict[str, Any]:
        sessions, status, per_agent_status, last_agent = self.overall_status(pending_session_ids)

        with self._lock:
            usage_snap = dict(self._usage_cache)
            codex_snap = dict(self._codex_usage_cache)
            copilot_snap = dict(self._copilot_usage_cache)

        if last_agent == "copilot" and copilot_snap:
            meter_agent = "copilot"
            usage = {
                "weekly_pct": copilot_snap.get("monthly_pct", 0.0),
                "weekly_reset": copilot_snap.get("monthly_reset", "--"),
                "weekly_reset_at": copilot_snap.get("monthly_reset_at", 0),
                "session_pct": 0.0,
                "session_reset": "--",
                "session_reset_at": 0,
            }
            weekly_title = "Copilot Monthly"
            session_title = ""
        elif last_agent == "codex" and codex_snap:
            meter_agent = "codex"
            usage = codex_snap
            weekly_title, session_title = self.agent_meter_titles(meter_agent)
        else:
            meter_agent = self._default_agent_id
            usage = usage_snap
            weekly_title, session_title = self.agent_meter_titles(meter_agent)

        per_agent_usage = {
            "claude": {
                **usage_snap,
                "agent_display": self.agent_display_name("claude"),
                "limits": [
                    self._usage_limit("Weekly", usage_snap, "weekly"),
                    self._usage_limit("Session", usage_snap, "session"),
                ],
            },
        }
        if codex_snap:
            per_agent_usage["codex"] = {
                **codex_snap,
                "agent_display": self.agent_display_name("codex"),
                "limits": [
                    self._usage_limit("Weekly", codex_snap, "weekly"),
                    self._usage_limit("Session", codex_snap, "session"),
                ],
            }
        if copilot_snap:
            per_agent_usage["copilot"] = {
                **copilot_snap,
                "agent_display": self.agent_display_name("copilot"),
            }

        return {
            **usage,
            "sessions": sessions,
            "status": status,
            "per_agent_status": per_agent_status,
            "per_agent_usage": per_agent_usage,
            "last_active_agent": last_agent,
            "agent_id": last_agent,
            "agent_display": self.agent_display_name(last_agent),
            "weekly_title": weekly_title,
            "session_title": session_title,
        }

    @staticmethod
    def _usage_limit(label: str, usage: dict[str, Any], prefix: str) -> dict[str, Any]:
        return {
            "label": label,
            "pct": usage.get(f"{prefix}_pct", 0.0),
            "reset": usage.get(f"{prefix}_reset", "--"),
            "reset_at": usage.get(f"{prefix}_reset_at", 0),
        }
