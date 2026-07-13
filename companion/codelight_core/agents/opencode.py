"""OpenCode integration (foundation slice: detection + cost meter).

OpenCode is BYOK — it has no provider quota, so unlike the other agents there
is no "% of a limit" to show; the only meaningful usage metric is cumulative
cost in $. OpenCode records `cost` per session in its SQLite store, so we sum
that for the current calendar month and render it against a user-set monthly
budget (opt-in `agents.opencode.monthly_budget_usd`). No pricing table needed.

Status, remote permission approval, and remote question answering come from
OpenCode's HTTP server SSE event bus, not from installed hooks — that
background-listener component is the next slice (see PLAN.md, OpenCode).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from codelight_core.agents import base
from codelight_core.timefmt import format_epoch_countdown


# Placeholder branding: a terminal-prompt ">_" mark (currentColor SVG +
# matching 48x48 1-bit bitmap for the screen). TODO(branding): swap for
# OpenCode's official logo and brand color when available.
_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" fill="none" '
    'stroke="currentColor" stroke-width="5" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M16 12 L32 24 L16 36"/>'
    '<line x1="18" y1="40" x2="34" y2="40"/></svg>'
)
_LOGO_BITMAP = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGAAAAAAAPAAAAAAAfwAAAAAAf4AAAAAAP8AAAAAAH/AAAAAAD/gAAAAAA/wAAAAAAf8AAAAAAP+AAAAAAD/AAAAAAB/wAAAAAA/4AAAAAAP8AAAAAAH+AAAAAAH+AAAAAAP8AAAAAA/4AAAAAB/wAAAAAD/AAAAAAP+AAAAAAf8AAAAAA/wAAAAAD/gAAAAAH/AAAAAAP8AAAAAAf4AAAAAAfwAAAAAAPAAAAAAAG//8AAAAA//8AAAAA//8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SPEC = base.AgentSpec(
    agent_id="opencode",
    display="OpenCode",
    executables=("opencode",),
    color="#6b7280",
    logo_svg=_LOGO_SVG,
    logo_bitmap=_LOGO_BITMAP,
)


def default_db_path() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data_home, "opencode", "opencode.db")


def _month_bounds(now: datetime) -> tuple[int, int]:
    """(start-of-this-month in ms, start-of-next-month in seconds), UTC.

    The ms value windows the SQLite query (OpenCode stores ms epochs); the
    seconds value is the meter's reset timestamp (clients/`format_epoch_
    countdown` use seconds)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return int(start.timestamp() * 1000), int(nxt.timestamp())


def month_cost_usd(db_path: str, *, now: datetime | None = None) -> float | None:
    """Sum of `session.cost` for the current calendar month, or None if the
    store can't be read. OpenCode books cost per session, so this needs no
    model pricing table."""
    if not os.path.isfile(db_path):
        return None
    start_ms, _ = _month_bounds(now or datetime.now(timezone.utc))
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM session "
                "WHERE time_created >= ?",
                (start_ms,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    try:
        return float(row[0] or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_usage(db_path: str, monthly_budget_usd: float,
              log: Callable[[str], None] | None = None) -> dict | None:
    """Monthly spend vs a self-set budget as a `monthly_pct` meter.

    Returns None when no budget is configured (nothing to meter) or the store
    can't be read — never raises. This is a tracking meter, not enforcement:
    codelight cannot cap OpenCode spend (the real bill is at the provider)."""
    if monthly_budget_usd <= 0:
        return None
    spent = month_cost_usd(db_path)
    if spent is None:
        return None
    _, reset_at = _month_bounds(datetime.now(timezone.utc))
    pct = max(0.0, min(1.0, spent / monthly_budget_usd))
    if log:
        log(f"[opencode-usage] ${spent:.2f}/${monthly_budget_usd:.2f} ({pct:.0%})")
    return {
        "monthly_pct": pct,
        "monthly_reset": format_epoch_countdown(reset_at),
        "monthly_reset_at": reset_at,
        "spent_usd": round(spent, 2),
        "budget_usd": monthly_budget_usd,
    }


class OpenCodeAgent:
    def __init__(self, db_path: str, monthly_budget_usd: float,
                 log: Callable[[str], None] | None = None) -> None:
        self.db_path = db_path
        self.monthly_budget_usd = monthly_budget_usd
        self.log = log

    def get_usage(self) -> dict | None:
        return get_usage(self.db_path, self.monthly_budget_usd, self.log)


def build_integration(config: dict, *,
                      log: Callable[[str], None] | None = None) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.opencode):
    db_path (SQLite store; default ~/.local/share/opencode/opencode.db);
    monthly_budget_usd (opt-in cost meter — this calendar month's spend vs this
    budget; the meter is hidden when unset)."""
    db_path = (os.path.expanduser(str(config.get("db_path") or ""))
               or default_db_path())
    try:
        budget = float(config.get("monthly_budget_usd") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    agent = OpenCodeAgent(db_path, budget, log)
    return base.AgentIntegration(
        spec=SPEC,
        agent=agent,
        # Opt-in $-budget meter; hidden (usage_fetcher=None) without a budget.
        usage_fetcher=agent.get_usage if budget > 0 else None,
        # No install_hooks: OpenCode has no hooks. Status + remote
        # permission/question answering will come from the server's SSE bus via
        # a background listener (next slice — needs AgentIntegration to grow an
        # optional long-lived listener component; see PLAN.md).
    )
