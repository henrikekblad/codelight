from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Callable

from codelight_core.timefmt import epoch, format_iso_countdown


USAGE_API = "https://claude.ai/api/oauth/usage"


class ClaudeAgent:
    def __init__(
        self,
        credentials_path: str,
        *,
        usage_api: str = USAGE_API,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.usage_api = usage_api
        self.log = log

    def get_usage(self) -> dict | None:
        return get_usage(
            self.credentials_path,
            usage_api=self.usage_api,
            log=self.log,
        )


def get_usage(
    credentials_path: str,
    *,
    usage_api: str = USAGE_API,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Fetch Claude OAuth usage.

    Returns session/weekly usage percentages and reset metadata, or None when
    credentials/API access are unavailable.
    """
    try:
        with open(credentials_path) as f:
            creds = json.load(f)
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception as e:
        print(f"[usage] could not read credentials: {e}", file=sys.stderr, flush=True)
        return None

    req = urllib.request.Request(
        usage_api,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/1.0.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"[usage] HTTP {e.code}: {e.reason}", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"[usage] request error: {e}", file=sys.stderr, flush=True)
        return None

    session = data.get("five_hour") or {}
    weekly = data.get("seven_day") or {}

    session_pct = float(session.get("utilization") or 0.0) / 100.0
    weekly_pct = float(weekly.get("utilization") or 0.0) / 100.0
    if log:
        log(f"[usage] API: session={session_pct:.0%} weekly={weekly_pct:.0%}")
    return {
        "session_pct": session_pct,
        "weekly_pct": weekly_pct,
        "session_reset": format_iso_countdown(session.get("resets_at", "")),
        "weekly_reset": format_iso_countdown(weekly.get("resets_at", "")),
        "session_reset_at": epoch(session.get("resets_at", "")),
        "weekly_reset_at": epoch(weekly.get("resets_at", "")),
    }
