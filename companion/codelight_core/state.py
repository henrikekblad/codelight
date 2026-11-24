from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


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
    agent_id: str = ""


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
        initial_last_active_agent: str | None = None,
        on_last_active_agent_changed: Callable[[str], None] | None = None,
        stale_after: float = 0.0,
        initial_agent_last_used: dict[str, float] | None = None,
        on_agent_used: Callable[[dict[str, float]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.RLock()
        self._default_agent_id = default_agent_id
        self._agent_registry = agent_registry
        self._idle_window = idle_window
        self._idle_window_waiting = idle_window_waiting
        self._sessions: dict[str, dict[str, Any]] = {}
        self._usage_caches: dict[str, dict[str, Any]] = {
            self._default_agent_id: dict(DEFAULT_USAGE),
        }
        self._last_transcript: dict[str, str] = {"sid": "", "path": ""}
        # agent_id → {"sid", "path"}: newest transcript seen per agent, so a
        # client can request any conversation-capable agent's latest feed.
        self._transcripts_by_agent: dict[str, dict[str, str]] = {}
        self._last_active_agent = self.normalize_agent_id(
            initial_last_active_agent)
        self._on_last_active_agent_changed = on_last_active_agent_changed
        # Configured agents shown as idle even without a usage meter or an
        # active session, so hook-only agents (no readable quota) stay visible.
        self._enabled_agents: set[str] = set()
        # Agents unused for longer than this drop out of client lists (0 =
        # keep forever). Last use is persisted so it survives restarts.
        self._stale_after = float(stale_after or 0.0)
        self._clock = clock
        self._agent_last_used: dict[str, float] = {
            self.normalize_agent_id(k): float(v)
            for k, v in (initial_agent_last_used or {}).items()
            if isinstance(v, (int, float))
        }
        self._persisted_last_used = dict(self._agent_last_used)
        self._on_agent_used = on_agent_used

    def set_enabled_agents(self, agent_ids) -> None:
        with self._lock:
            self._enabled_agents = {
                self.normalize_agent_id(a) for a in (agent_ids or set())
            }

    def seed_agent_last_used(self, seeds: dict[str, float]) -> None:
        """Give agents with no recorded use a starting point (e.g. their newest
        transcript, or "now" so nothing vanishes right after an upgrade)."""
        changed = False
        with self._lock:
            for agent_id, ts in seeds.items():
                aid = self.normalize_agent_id(agent_id)
                if aid not in self._agent_last_used:
                    self._agent_last_used[aid] = float(ts)
                    changed = True
            snapshot = dict(self._agent_last_used)
            if changed:
                self._persisted_last_used = dict(snapshot)
        if changed and self._on_agent_used:
            self._on_agent_used(snapshot)

    def agent_last_used(self) -> dict[str, float]:
        with self._lock:
            return dict(self._agent_last_used)

    def _note_agent_used(self, agent_id: str) -> dict[str, float] | None:
        """Record use; return a snapshot to persist when it moved enough to be
        worth a disk write (hourly granularity is plenty for a 30-day rule)."""
        now = self._clock()
        self._agent_last_used[agent_id] = now
        if now - self._persisted_last_used.get(agent_id, 0.0) < 3600:
            return None
        self._persisted_last_used[agent_id] = now
        return dict(self._agent_last_used)

    def _is_stale(self, agent_id: str, now: float) -> bool:
        if self._stale_after <= 0:
            return False
        last = self._agent_last_used.get(agent_id)
        return last is not None and now - last > self._stale_after

    def normalize_agent_id(self, agent_id: str | None) -> str:
        aid = str(agent_id or "").strip().lower()
        return aid if aid else self._default_agent_id

    @property
    def default_agent_id(self) -> str:
        return self._default_agent_id

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
        persist_last_agent = False
        used_snapshot = None
        with self._lock:
            if state in ("working", "waiting"):
                used_snapshot = self._note_agent_used(normalized_agent)
            if transcript:
                self._last_transcript = {
                    "sid": session_id,
                    "path": transcript,
                    "agent_id": normalized_agent,
                }
                self._transcripts_by_agent[normalized_agent] = {
                    "sid": session_id,
                    "path": transcript,
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
            # Waiting prompts should be shown while they are pending, but they
            # should not permanently hijack the idle fallback once resolved.
            # Only actual work marks an agent as the last used one.
            if state == "working" and normalized_agent != self._last_active_agent:
                self._last_active_agent = normalized_agent
                persist_last_agent = True
        if persist_last_agent and self._on_last_active_agent_changed:
            self._on_last_active_agent_changed(normalized_agent)
        if used_snapshot is not None and self._on_agent_used:
            self._on_agent_used(used_snapshot)

    def active_transcript(self) -> ActiveTranscript:
        with self._lock:
            best: tuple[str, float, str, str] | None = None
            for sid, info in self._sessions.items():
                transcript = str(info.get("transcript") or "")
                if transcript and (best is None or float(info["time"]) > best[1]):
                    best = (sid, float(info["time"]), transcript,
                            self.normalize_agent_id(info.get("agent_id")))
            if best:
                # Agent travels with the transcript so the conversation label
                # can never diverge from the content being shown.
                return ActiveTranscript(best[0], best[2], best[3])
            path = self._last_transcript.get("path", "")
            if path:
                return ActiveTranscript(
                    self._last_transcript.get("sid", ""),
                    path,
                    self.normalize_agent_id(
                        self._last_transcript.get("agent_id")),
                )
        return ActiveTranscript("", "")

    def transcript_for_agent(self, agent_id: str) -> ActiveTranscript:
        """Latest known transcript for a specific agent: an active session's
        first, else the newest transcript that agent ever reported this run."""
        aid = self.normalize_agent_id(agent_id)
        with self._lock:
            best: tuple[str, float, str] | None = None
            for sid, info in self._sessions.items():
                if self.normalize_agent_id(info.get("agent_id")) != aid:
                    continue
                transcript = str(info.get("transcript") or "")
                if transcript and (best is None or float(info["time"]) > best[1]):
                    best = (sid, float(info["time"]), transcript)
            if best:
                return ActiveTranscript(best[0], best[2], aid)
            rec = self._transcripts_by_agent.get(aid)
            if rec and rec.get("path"):
                return ActiveTranscript(rec.get("sid", ""), rec["path"], aid)
        return ActiveTranscript("", "", aid)

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
            selected_agent = last_agent
            selected_time = 0.0
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
                event_time = float(info["time"])
                if event_time > selected_time:
                    selected_agent = agent_id
                    selected_time = event_time
            # Every configured agent stays visible (idle) even with no active
            # session — agents without a usage meter would otherwise vanish —
            # unless it hasn't been used for a long time.
            clock_now = self._clock()
            for agent_id in self._enabled_agents:
                if not self._is_stale(agent_id, clock_now):
                    per_agent.setdefault(agent_id, "idle")
            if not per_agent:
                per_agent[selected_agent] = "idle"
            return active, overall, per_agent, selected_agent

    def update_usage(
        self,
        *,
        usages: dict[str, dict[str, Any] | None] | None = None,
        clear_missing: bool = False,
    ) -> None:
        """Update per-agent usage caches.

        A ``None`` usage clears that agent's cache (the default agent resets
        to ``DEFAULT_USAGE`` instead of disappearing).
        """
        incoming: dict[str, dict[str, Any] | None] = dict(usages or {})

        with self._lock:
            if clear_missing:
                for agent_id in list(self._usage_caches):
                    if agent_id == self._default_agent_id:
                        continue
                    if agent_id not in incoming:
                        del self._usage_caches[agent_id]
            for agent_id, usage in incoming.items():
                aid = self.normalize_agent_id(agent_id)
                if usage is None:
                    if aid == self._default_agent_id:
                        self._usage_caches[aid] = dict(DEFAULT_USAGE)
                    else:
                        self._usage_caches.pop(aid, None)
                    continue
                current = self._usage_caches.setdefault(
                    aid,
                    dict(DEFAULT_USAGE) if aid == self._default_agent_id else {},
                )
                current.update(usage)

    def _meter_usage(self, agent_id: str,
                     usage: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        if "monthly_pct" in usage and "weekly_pct" not in usage:
            display = self.agent_display_name(agent_id)
            return (
                {
                    "weekly_pct": usage.get("monthly_pct", 0.0),
                    "weekly_reset": usage.get("monthly_reset", "--"),
                    "weekly_reset_at": usage.get("monthly_reset_at", 0),
                    "session_pct": 0.0,
                    "session_reset": "--",
                    "session_reset_at": 0,
                },
                f"{display} Monthly",
                "",
            )
        weekly_title, session_title = self.agent_meter_titles(agent_id)
        # Blank the title of a window the agent doesn't report so the screen
        # hides that bar (it draws a meter only when its title is non-empty).
        if "weekly_pct" not in usage:
            weekly_title = ""
        if "session_pct" not in usage:
            session_title = ""
        return dict(usage), weekly_title, session_title

    def _per_agent_usage(self, snapshots: dict[str, dict[str, Any]],
                         active: set[str] | None = None) -> dict[str, Any]:
        per_agent_usage: dict[str, Any] = {}
        now = self._clock()
        with self._lock:
            stale = {aid for aid in snapshots
                     if aid not in (active or set()) and self._is_stale(aid, now)}
        for agent_id, usage in snapshots.items():
            if not usage or agent_id in stale:
                continue
            entry = {
                **usage,
                "agent_display": self.agent_display_name(agent_id),
            }
            limits = self._usage_limits(usage)
            if limits:
                entry["limits"] = limits
            per_agent_usage[agent_id] = entry
        return per_agent_usage

    def status_snapshot(self, pending_session_ids: set[str] | None = None) -> dict[str, Any]:
        sessions, status, per_agent_status, last_agent = self.overall_status(pending_session_ids)

        with self._lock:
            usage_snaps = {
                agent_id: dict(usage)
                for agent_id, usage in self._usage_caches.items()
            }

        last_usage = usage_snaps.get(last_agent)
        if last_usage:
            usage, weekly_title, session_title = self._meter_usage(
                last_agent, last_usage)
        else:
            # No usage info for the active agent: empty titles tell meter
            # clients (the screen) to hide the bars rather than show another
            # agent's numbers.
            usage = dict(DEFAULT_USAGE)
            weekly_title = ""
            session_title = ""
        per_agent_usage = self._per_agent_usage(
            usage_snaps, active=set(per_agent_status) | {last_agent})

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
    def _usage_limits(usage: dict[str, Any]) -> list[dict[str, Any]]:
        # A meter is shown only for a window the agent actually reports, so a
        # limit the plan no longer has (e.g. Codex's 5-hour session window after
        # OpenAI's 2026-07 weekly-only change) disappears instead of showing 0%,
        # and reappears on its own once the window returns.
        limits = []
        if "weekly_pct" in usage:
            limits.append(CodelightState._usage_limit("Weekly", usage, "weekly"))
        if "session_pct" in usage:
            limits.append(CodelightState._usage_limit("Session", usage, "session"))
        if limits:
            return limits
        if "monthly_pct" in usage:
            return [CodelightState._usage_limit("Monthly", usage, "monthly")]
        return []

    @staticmethod
    def _usage_limit(label: str, usage: dict[str, Any], prefix: str) -> dict[str, Any]:
        return {
            "label": label,
            "pct": usage.get(f"{prefix}_pct", 0.0),
            "reset": usage.get(f"{prefix}_reset", "--"),
            "reset_at": usage.get(f"{prefix}_reset_at", 0),
        }

    def set_agent_capability(
        self,
        agent_id: str,
        key: str,
        value: Any,
    ) -> None:
        aid = self.normalize_agent_id(agent_id)
        with self._lock:
            usage = self._usage_caches.setdefault(
                aid,
                dict(DEFAULT_USAGE) if aid == self._default_agent_id else {},
            )
            usage[key] = value
