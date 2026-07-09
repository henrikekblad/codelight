from __future__ import annotations

import time
from datetime import datetime, timezone


def format_countdown(diff_secs: int) -> str:
    if diff_secs <= 0:
        return "--"
    days = diff_secs // 86400
    hours = (diff_secs % 86400) // 3600
    mins = (diff_secs % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def epoch(iso_ts: str) -> int:
    if not iso_ts:
        return 0
    try:
        return int(datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def format_iso_countdown(iso_ts: str) -> str:
    if not iso_ts:
        return "--"
    try:
        target = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        diff = int((target - datetime.now(timezone.utc)).total_seconds())
        return format_countdown(diff)
    except Exception:
        return "--"


def format_epoch_countdown(epoch_seconds: int) -> str:
    try:
        diff = int(epoch_seconds) - int(time.time())
        return format_countdown(diff)
    except (TypeError, ValueError):
        return "--"
