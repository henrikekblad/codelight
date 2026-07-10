from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from codelight_core.timefmt import format_epoch_countdown


def events_path_for_session(copilot_home: str, session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    base = os.path.realpath(os.path.join(copilot_home, "session-state"))
    path = os.path.realpath(os.path.join(base, sid, "events.jsonl"))
    if not path.startswith(base + os.sep):
        return ""
    return path if os.path.isfile(path) else ""


def latest_events_path(copilot_home: str) -> str:
    try:
        base = os.path.join(copilot_home, "session-state")
        newest_path = ""
        newest_mtime = 0.0
        for root, _, files in os.walk(base):
            if "events.jsonl" not in files:
                continue
            path = os.path.join(root, "events.jsonl")
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


class CopilotAgent:
    def __init__(
        self,
        org: str = "",
        *,
        copilot_home: str = "",
        token_file: str = "",
        api: Callable[[str, str], dict] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.org = org
        self.copilot_home = copilot_home
        self.token_file = token_file
        self.api = api or github_api
        self.log = log

    def token(self) -> str:
        return github_token(self.token_file)

    def get_usage(
        self,
        *,
        org: str | None = None,
        token: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        resolved_org = (org if org is not None else self.org).strip()
        resolved_token = token if token is not None else self.token()
        if not resolved_org or not resolved_token:
            return None
        return get_usage(
            resolved_org,
            resolved_token,
            now or datetime.now(timezone.utc),
            api=self.api,
            log=self.log,
        )

    def events_path_for_session(self, session_id: str) -> str:
        return events_path_for_session(self.copilot_home, session_id)

    def latest_events_path(self) -> str:
        return latest_events_path(self.copilot_home)


def github_token(token_file: str = "") -> str:
    """Resolve a GitHub token without making the gh CLI a requirement."""
    for key in ("CODELIGHT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(key, "").strip()
        if token:
            return token
    if token_file:
        try:
            with open(os.path.expanduser(token_file)) as stream:
                return stream.read().strip()
        except Exception:
            pass
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
    return ""


def github_api(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "codelight",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def next_month_start(now: datetime) -> int:
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


def get_usage(
    org: str,
    token: str,
    now: datetime,
    *,
    api: Callable[[str, str], dict] = github_api,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Fetch the organization's pooled monthly Copilot AI-credit usage."""
    org = org.strip()
    if not org or not token:
        return None
    query = urllib.parse.urlencode({"year": now.year, "month": now.month})
    try:
        billing = api(
            f"organizations/{urllib.parse.quote(org)}/settings/billing/"
            f"ai_credit/usage?{query}", token)
    except urllib.error.HTTPError as e:
        if log:
            log(f"[copilot-usage] billing unavailable for {org}: HTTP {e.code}")
        return None
    except Exception as e:
        if log:
            log(f"[copilot-usage] billing request failed: {e}")
        return None
    try:
        subscription = api(
            f"orgs/{urllib.parse.quote(org)}/copilot/billing", token)
    except urllib.error.HTTPError as e:
        if log:
            log(f"[copilot-usage] subscription unavailable for {org}: HTTP {e.code}")
        return None
    except Exception as e:
        if log:
            log(f"[copilot-usage] subscription request failed: {e}")
        return None

    try:
        used = sum(
            float(item.get("grossQuantity") or 0.0)
            for item in billing.get("usageItems", [])
            if item.get("product") == "Copilot"
            and str(item.get("unitType", "")).lower() in
            {"credits", "ai-credits"}
        )
        seats = int(subscription.get("seat_breakdown", {}).get("total") or 0)
        plan = str(subscription.get("plan_type") or "business").lower()
    except (TypeError, ValueError):
        return None
    if seats <= 0:
        return None

    promotional = datetime(2026, 6, 1, tzinfo=timezone.utc) <= now < \
        datetime(2026, 9, 1, tzinfo=timezone.utc)
    per_seat = (
        7000 if promotional and plan == "enterprise"
        else 3000 if promotional
        else 3900 if plan == "enterprise"
        else 1900
    )
    allowance = seats * per_seat
    reset_at = next_month_start(now)
    pct = max(0.0, min(1.0, used / allowance))
    if log:
        log(f"[copilot-usage] {org}: {used:.0f}/{allowance} credits ({pct:.0%})")
    return {
        "monthly_pct": pct,
        "monthly_reset": format_epoch_countdown(reset_at),
        "monthly_reset_at": reset_at,
        "used_credits": used,
        "included_credits": allowance,
        "plan_type": plan,
        "seat_count": seats,
        "limits": [{
            "label": "Monthly",
            "pct": pct,
            "reset": format_epoch_countdown(reset_at),
            "reset_at": reset_at,
        }],
    }
