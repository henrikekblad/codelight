"""Claude account profiles: remember every Claude login seen on this machine
and swap between them.

Claude Code keeps exactly one login: the OAuth blob in
``~/.claude/.credentials.json`` plus the account metadata under
``oauthAccount`` in ``~/.claude.json``. Everything else — transcripts, memory,
settings — is local and account-independent, so switching accounts only means
swapping those two pieces.

Profiles are captured automatically: whenever the active login differs from
its stored snapshot (a new ``/login``, or Claude Code rotating its tokens) the
snapshot is updated. Log in once with each account and both become
switchable.

Token ownership: the *active* login belongs to Claude Code, which refreshes it
and rotates its refresh token — codelight never refreshes it, and always saves
the latest copy back to the profile before switching away (a stale refresh
token would be dead). *Inactive* profiles are owned by codelight, which
refreshes them when it needs to read their usage.

Not supported on macOS, where the CLI keeps the login in the keychain.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from codelight_core.timefmt import epoch, format_iso_countdown


TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code's public OAuth client id (the refresh tokens were issued to it).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Read inactive accounts less often than the active one; they change only
# when used elsewhere (claude.ai, another machine).
INACTIVE_USAGE_TTL = 300
# Refresh an inactive token this long before it expires.
REFRESH_MARGIN = 120

Logger = Callable[[str], None]
Http = Callable[..., Any]


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        _err(f"[claude-accounts] could not read {path}: {e}")
        return None


def _write_json_atomic(path: str, data: dict, mode: int = 0o600) -> None:
    """Write via a temp file in the same directory and rename over ``path``,
    keeping the file private. Readers never see a half-written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.codelight.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def usage_from_api(data: dict) -> dict:
    """Map the /api/oauth/usage response onto codelight's usage fields."""
    session = data.get("five_hour") or {}
    weekly = data.get("seven_day") or {}
    return {
        "session_pct": float(session.get("utilization") or 0.0) / 100.0,
        "weekly_pct": float(weekly.get("utilization") or 0.0) / 100.0,
        "session_reset": format_iso_countdown(session.get("resets_at", "")),
        "weekly_reset": format_iso_countdown(weekly.get("resets_at", "")),
        "session_reset_at": epoch(session.get("resets_at", "")),
        "weekly_reset_at": epoch(weekly.get("resets_at", "")),
    }


def usage_limits(usage: dict | None) -> list[dict]:
    """Client-facing meter list, same shape as ``per_agent_usage[*].limits``."""
    if not usage:
        return []
    return [
        {"label": label, "pct": usage.get(f"{key}_pct", 0.0),
         "reset": usage.get(f"{key}_reset", "--"),
         "reset_at": usage.get(f"{key}_reset_at", 0)}
        for label, key in (("Weekly", "weekly"), ("Session", "session"))
    ]


def fetch_usage(token: str, usage_api: str, *, http: Http = urllib.request.urlopen,
                tag: str = "usage") -> dict | None:
    req = urllib.request.Request(
        usage_api,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/1.0.0",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with http(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _err(f"[{tag}] HTTP {e.code}: {e.reason}")
        return None
    except Exception as e:
        _err(f"[{tag}] request error: {e}")
        return None
    return usage_from_api(data if isinstance(data, dict) else {})


def account_id_for(credentials: dict | None, oauth_account: dict | None) -> str:
    """Stable profile id: one per Claude organization (a Team seat and a
    personal Pro/Max plan live in different organizations, even for the same
    person). Empty when the login can't be identified."""
    org = str((credentials or {}).get("organizationUuid") or "")
    if not org and oauth_account:
        org = str(oauth_account.get("organizationUuid") or "")
    if not org:
        return ""
    return hashlib.sha256(org.encode()).hexdigest()[:12]


class ClaudeAccounts:
    def __init__(
        self,
        credentials_path: str,
        claude_json_path: str,
        store_dir: str,
        *,
        usage_api: str,
        labels: dict[str, str] | None = None,
        token_url: str = TOKEN_URL,
        client_id: str = CLIENT_ID,
        http: Http = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        log: Logger | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.claude_json_path = claude_json_path
        self.store_dir = store_dir
        self.usage_api = usage_api
        self.labels = {str(k).lower(): str(v) for k, v in (labels or {}).items()}
        self.token_url = token_url
        self.client_id = client_id
        self.http = http
        self.clock = clock
        self.log = log or (lambda _m: None)
        self._lock = threading.RLock()
        # account id → (fetched_at, usage) for inactive accounts.
        self._inactive_usage: dict[str, tuple[float, dict | None]] = {}
        self._last_touch = 0.0

    # ── Profile storage ────────────────────────────────────────────────────

    def _profile_path(self, account_id: str) -> str:
        return os.path.join(self.store_dir, f"{account_id}.json")

    def _load_profile(self, account_id: str) -> dict | None:
        return _read_json(self._profile_path(account_id))

    def _save_profile(self, profile: dict) -> None:
        os.makedirs(self.store_dir, mode=0o700, exist_ok=True)
        _write_json_atomic(self._profile_path(profile["id"]), profile)

    def profiles(self) -> list[dict]:
        try:
            names = sorted(os.listdir(self.store_dir))
        except FileNotFoundError:
            return []
        out = []
        for name in names:
            if name.endswith(".json") and not name.startswith("."):
                profile = _read_json(os.path.join(self.store_dir, name))
                if profile and profile.get("id") and profile.get("credentials"):
                    out.append(profile)
        return out

    def _oauth_account(self) -> dict | None:
        data = _read_json(self.claude_json_path)
        account = (data or {}).get("oauthAccount")
        return account if isinstance(account, dict) else None

    # ── Active login ───────────────────────────────────────────────────────

    def sync(self) -> str:
        """Snapshot the active login into its profile; return its id ("" if
        there is no identifiable login)."""
        with self._lock:
            creds = _read_json(self.credentials_path)
            if not creds or not isinstance(creds.get("claudeAiOauth"), dict):
                return ""
            oauth = self._oauth_account()
            account_id = account_id_for(creds, oauth)
            if not account_id:
                return ""
            profile = self._load_profile(account_id) or {
                "id": account_id, "last_used": self.clock()}
            changed = profile.get("credentials") != creds
            # ~/.claude.json can lag behind (or belong to another login while
            # a switch is half-done): only take metadata that matches.
            if oauth and account_id_for(None, oauth) == account_id \
                    and profile.get("oauth_account") != oauth:
                profile["oauth_account"] = oauth
                changed = True
            if changed:
                profile["credentials"] = creds
                profile["saved_at"] = self.clock()
                self._save_profile(profile)
            return account_id

    def touch_active(self) -> None:
        """Record that the active account was just used. Called from agent
        hook events, so it does real work at most every ten minutes."""
        if self.clock() - self._last_touch < 600:
            return
        self._last_touch = self.clock()
        with self._lock:
            account_id = self.sync()
            if not account_id:
                return
            profile = self._load_profile(account_id)
            if profile is None:
                return
            now = self.clock()
            # Coarse: rewriting the profile on every hook event is pointless.
            if now - float(profile.get("last_used") or 0) < 3600:
                return
            profile["last_used"] = now
            self._save_profile(profile)

    def active_token(self) -> str:
        creds = _read_json(self.credentials_path) or {}
        return str((creds.get("claudeAiOauth") or {}).get("accessToken") or "")

    # ── Inactive tokens ────────────────────────────────────────────────────

    def _fresh_token(self, profile: dict) -> str:
        """Access token of an inactive profile, refreshed when about to
        expire. Saves the rotated tokens back into the profile."""
        oauth = profile["credentials"].get("claudeAiOauth") or {}
        token = str(oauth.get("accessToken") or "")
        expires_at = float(oauth.get("expiresAt") or 0) / 1000.0
        if token and expires_at - REFRESH_MARGIN > self.clock():
            return token
        refresh = str(oauth.get("refreshToken") or "")
        if not refresh:
            return ""
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": self.client_id,
        }
        if oauth.get("scopes"):
            body["scope"] = " ".join(oauth["scopes"])
        req = urllib.request.Request(
            self.token_url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "claude-code/1.0.0"},
            method="POST",
        )
        try:
            with self.http(req, timeout=15) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            _err(f"[claude-accounts] token refresh for {profile['id']} failed: "
                 f"HTTP {e.code} — log in with that account again")
            return ""
        except Exception as e:
            _err(f"[claude-accounts] token refresh for {profile['id']} failed: {e}")
            return ""
        token = str(data.get("access_token") or "")
        if not token:
            return ""
        now_ms = int(self.clock() * 1000)
        oauth = dict(oauth)
        oauth["accessToken"] = token
        oauth["refreshToken"] = str(data.get("refresh_token") or refresh)
        oauth["expiresAt"] = now_ms + int(data.get("expires_in") or 3600) * 1000
        if data.get("refresh_token_expires_in"):
            oauth["refreshTokenExpiresAt"] = (
                now_ms + int(data["refresh_token_expires_in"]) * 1000)
        creds = dict(profile["credentials"])
        creds["claudeAiOauth"] = oauth
        profile["credentials"] = creds
        profile["saved_at"] = self.clock()
        self._save_profile(profile)
        self.log(f"[claude-accounts] refreshed token for {self.label(profile)}")
        return token

    # ── Presentation ───────────────────────────────────────────────────────

    def label(self, profile: dict) -> str:
        oauth = profile.get("oauth_account") or {}
        email = str(oauth.get("emailAddress") or "")
        org = str(oauth.get("organizationName") or "")
        for key in (profile.get("id", ""), email.lower(), org.lower()):
            if key and key in self.labels:
                return self.labels[key]
        org_type = str(oauth.get("organizationType") or "")
        # Team/Enterprise seats are best known by their organization; personal
        # plans live in an auto-named org ("…'s Organization"), so use email.
        if org and org_type in ("claude_team", "claude_enterprise"):
            return org
        return email or profile.get("id", "Claude")

    def poll(self, *, stale_after: float = 0.0) -> tuple[dict | None, list[dict]]:
        """(active account usage, account list for clients).

        The list includes every profile used within ``stale_after`` seconds
        (0 = no limit) plus the active one, so an account left unused for a
        month drops out of the clients' lists.
        """
        with self._lock:
            active_id = self.sync()
            token = self.active_token()
            active_usage = fetch_usage(token, self.usage_api, http=self.http) \
                if token else None
            now = self.clock()
            accounts = []
            for profile in self.profiles():
                pid = profile["id"]
                is_active = pid == active_id
                if not is_active and stale_after > 0 \
                        and now - float(profile.get("last_used") or 0) > stale_after:
                    continue
                if is_active:
                    usage = active_usage
                else:
                    cached = self._inactive_usage.get(pid)
                    if cached and now - cached[0] < INACTIVE_USAGE_TTL:
                        usage = cached[1]
                    else:
                        inactive_token = self._fresh_token(profile)
                        usage = fetch_usage(
                            inactive_token, self.usage_api, http=self.http,
                            tag=f"usage:{pid}") if inactive_token else None
                        self._inactive_usage[pid] = (now, usage)
                oauth = profile.get("oauth_account") or {}
                accounts.append({
                    "id": pid,
                    "label": self.label(profile),
                    "email": str(oauth.get("emailAddress") or ""),
                    "organization": str(oauth.get("organizationName") or ""),
                    "active": is_active,
                    "available": usage is not None,
                    "limits": usage_limits(usage),
                })
            accounts.sort(key=lambda a: (not a["active"], a["label"].lower()))
            return active_usage, accounts

    # ── Switching ──────────────────────────────────────────────────────────

    def switch(self, account_id: str) -> dict:
        with self._lock:
            active_id = self.sync()
            target = self._load_profile(str(account_id or ""))
            if target is None or not target.get("credentials"):
                return {"ok": False, "outcome": "unknown_account",
                        "message": "No saved login for that account."}
            label = self.label(target)
            if target["id"] == active_id:
                return {"ok": True, "outcome": "already_active",
                        "account_id": target["id"], "account_label": label}
            # Hand Claude Code a token that is valid now; from here on it owns
            # the refreshing.
            if not self._fresh_token(target):
                return {"ok": False, "outcome": "login_expired",
                        "account_id": target["id"], "account_label": label,
                        "message": f"The saved login for {label} has expired. "
                                   "Run `claude /login` with that account once."}
            target = self._load_profile(target["id"]) or target
            _write_json_atomic(self.credentials_path, target["credentials"])
            oauth = target.get("oauth_account")
            if oauth:
                claude_json = _read_json(self.claude_json_path)
                if claude_json is not None:
                    claude_json["oauthAccount"] = oauth
                    mode = 0o600
                    try:
                        mode = os.stat(self.claude_json_path).st_mode & 0o777
                    except OSError:
                        pass
                    _write_json_atomic(self.claude_json_path, claude_json, mode)
            target["last_used"] = self.clock()
            self._save_profile(target)
            # The previously active account is now inactive: read it fresh.
            self._inactive_usage.pop(active_id, None)
            self._inactive_usage.pop(target["id"], None)
            self.log(f"[claude-accounts] switched to {label}")
            return {"ok": True, "outcome": "switched",
                    "account_id": target["id"], "account_label": label}
