from __future__ import annotations

from datetime import datetime
from typing import Iterable


STATUS_COLOR = {
    "working": "\033[33m",   # orange
    "waiting": "\033[31m",   # red
    "idle": "\033[32m",      # green
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BAR_W = 28


def _agent_display_name(agent_registry: dict[str, dict[str, str]],
                        default_agent_id: str,
                        agent_id: str | None) -> str:
    aid = str(agent_id or "").strip().lower() or default_agent_id
    if aid in agent_registry:
        return agent_registry[aid].get("display", aid.capitalize())
    return aid.capitalize() if aid else agent_registry[default_agent_id]["display"]


def usage_bar(pct: float, width: int = BAR_W) -> str:
    filled = round(max(0.0, min(1.0, pct)) * width)
    return "█" * filled + "░" * (width - filled)


def dashboard_lines(
    payload: dict,
    *,
    agent_registry: dict[str, dict[str, str]],
    default_agent_id: str,
    log_lines: Iterable[str],
    ws_count: int,
    dbus_present: bool,
    now: datetime | None = None,
) -> list[str]:
    status = str(payload.get("status") or "idle")
    color = STATUS_COLOR.get(status, "")
    ts = (now or datetime.now()).strftime("%H:%M:%S")

    parts: list[str] = []
    if ws_count:
        parts.append(f"{ws_count} WebSocket{'s' if ws_count != 1 else ''}")
    if dbus_present:
        parts.append("D-Bus")
    clients_str = "  ".join(parts) if parts else "none"

    sessions = int(payload.get("sessions") or 0)
    per_agent_status = payload.get("per_agent_status")
    if not isinstance(per_agent_status, dict):
        per_agent_status = {}
    per_agent_usage = payload.get("per_agent_usage")
    if not isinstance(per_agent_usage, dict):
        per_agent_usage = {}

    agent_lines: list[str] = []
    for aid in agent_registry.keys():
        if aid not in per_agent_usage and aid not in per_agent_status:
            continue
        display = _agent_display_name(agent_registry, default_agent_id, aid)
        astate = str(per_agent_status.get(aid, "idle"))
        acolor = STATUS_COLOR.get(astate, "")

        usage = per_agent_usage.get(aid, {})
        limits = usage.get("limits") if isinstance(usage, dict) else None
        agent_lines.append(
            f"  {acolor}● {BOLD}{display}{RESET} "
            f"{DIM}{astate.upper()}{RESET}"
        )
        if not isinstance(limits, list):
            limits = []
        rendered_limit = False
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            rendered_limit = True
            pct_raw = limit.get("pct", 0.0)
            try:
                pct = float(pct_raw)
            except (TypeError, ValueError):
                pct = 0.0
            label = str(limit.get("label") or "Limit")
            reset = str(limit.get("reset") or "--")
            agent_lines.append(
                f"    {label:<8} {usage_bar(pct)} {pct:>4.0%}"
                f"  {DIM}resets {reset}{RESET}"
            )
        if not rendered_limit:
            agent_lines.append(f"    {DIM}No usage data{RESET}")
        agent_lines.append("")

    return [
        f"{BOLD}CODELIGHT{RESET}",
        f"  Updated:  {ts}",
        f"  Clients:  {clients_str}",
        "",
        f"  {color}● {status.upper()}{RESET}  "
        f"{DIM}({sessions} session{'s' if sessions != 1 else ''}){RESET}",
        "",
        f"  {DIM}Agents{RESET}",
    ] + agent_lines + [
        f"  {DIM}Recent activity{RESET}",
    ] + [f"  {ln}" for ln in log_lines]


def render_dashboard(
    payload: dict,
    *,
    agent_registry: dict[str, dict[str, str]],
    default_agent_id: str,
    log_lines: Iterable[str],
    ws_count: int,
    dbus_present: bool,
    dashboard_ready: bool,
    now: datetime | None = None,
) -> str:
    lines = dashboard_lines(
        payload,
        agent_registry=agent_registry,
        default_agent_id=default_agent_id,
        log_lines=log_lines,
        ws_count=ws_count,
        dbus_present=dbus_present,
        now=now,
    )
    prefix = "\033[2J\033[H" if not dashboard_ready else "\033[H"
    cleared = [ln + "\033[K" for ln in lines]
    return prefix + "\n".join(cleared) + "\033[J"
