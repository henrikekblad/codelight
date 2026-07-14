from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from typing import Callable

from codelight_core import hooks as hooks_core
from codelight_core import transcript as transcript_core
from codelight_core.agents import base
from codelight_core.timefmt import format_epoch_countdown


LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid" viewBox="0 0 256 260">'
    '<path fill="currentColor" d="M239.184 106.203a64.716 64.716 0 0 0-5.576-53.103C219.452 28.459 191 15.784 163.213 21.74A65.586 65.586 0 0 0 52.096 45.22a64.716 64.716 0 0 0-43.23 31.36c-14.31 24.602-11.061 55.634 8.033 76.74a64.665 64.665 0 0 0 5.525 53.102c14.174 24.65 42.644 37.324 70.446 31.36a64.72 64.72 0 0 0 48.754 21.744c28.481.025 53.714-18.361 62.414-45.481a64.767 64.767 0 0 0 43.229-31.36c14.137-24.558 10.875-55.423-8.083-76.483Zm-97.56 136.338a48.397 48.397 0 0 1-31.105-11.255l1.535-.87 51.67-29.825a8.595 8.595 0 0 0 4.247-7.367v-72.85l21.845 12.636c.218.111.37.32.409.563v60.367c-.056 26.818-21.783 48.545-48.601 48.601Zm-104.466-44.61a48.345 48.345 0 0 1-5.781-32.589l1.534.921 51.722 29.826a8.339 8.339 0 0 0 8.441 0l63.181-36.425v25.221a.87.87 0 0 1-.358.665l-52.335 30.184c-23.257 13.398-52.97 5.431-66.404-17.803ZM23.549 85.38a48.499 48.499 0 0 1 25.58-21.333v61.39a8.288 8.288 0 0 0 4.195 7.316l62.874 36.272-21.845 12.636a.819.819 0 0 1-.767 0L41.353 151.53c-23.211-13.454-31.171-43.144-17.804-66.405v.256Zm179.466 41.695-63.08-36.63L161.73 77.86a.819.819 0 0 1 .768 0l52.233 30.184a48.6 48.6 0 0 1-7.316 87.635v-61.391a8.544 8.544 0 0 0-4.4-7.213Zm21.742-32.69-1.535-.922-51.619-30.081a8.39 8.39 0 0 0-8.492 0L99.98 99.808V74.587a.716.716 0 0 1 .307-.665l52.233-30.133a48.652 48.652 0 0 1 72.236 50.391v.205ZM88.061 139.097l-21.845-12.585a.87.87 0 0 1-.41-.614V65.685a48.652 48.652 0 0 1 79.757-37.346l-1.535.87-51.67 29.825a8.595 8.595 0 0 0-4.246 7.367l-.051 72.697Zm11.868-25.58 28.138-16.217 28.188 16.218v32.434l-28.086 16.218-28.188-16.218-.052-32.434Z"/>'
    '</svg>'
)

# 48x48 1-bit render of LOGO_SVG for the ESP8266 screen.
LOGO_BITMAP = (
    "AAB/wAAAAAH/8AAAAAf/+AAAAA///uAAAB+AP/wAAB8AP/8AAD4A//+AADwB/h/AAPwH+AP"
    "gA/gf4AHwD/h/gAD4H/j+A4B4P/j8D8B4PnjwH/A8fHjgf/w8eHjh+P888Hjn4H+88Hjv8B"
    "/88Hj//Af48Hj8PwH48HjwD8D88HjgB+A+8HjgB/ge8HjgB34f+H7gBx4PeB/gBx4PfAfgB"
    "x4PPwHwDx4PH4D8Px4PH+A//x4PP/gP9x4PPf4Hxx4PPH8fhx4ePD/+Bx4+PA/4Dx58HgPg"
    "Px/4HgDAfx/4DwAB/h/gD4AH+B/AB+Af4D4AA/j/gDwAAf//AHwAAP/8APgAAD/+A/AAAAI"
    "///AAAAAf/8AAAAAP/4AAAAAD/gAA"
)

SPEC = base.AgentSpec(
    "codex",
    "Codex",
    executables=("codex",),
    vscode_extensions=frozenset({"openai.chatgpt"}),
    color="#FFFFFF",
    logo_svg=LOGO_SVG,
    logo_bitmap=LOGO_BITMAP,
)

HOOK_MODES = (
    base.HookMode("question-codex", kind="question",
                  envelope=base.CONTEXT, default_agent_id="codex"),
)


def default_home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def tool_result_text(content) -> str:
    """Remove Codex's execution envelope from a tool result."""
    if not isinstance(content, str):
        return transcript_core.tool_result_text(content)
    lines = content.strip().splitlines()
    while lines and (
        lines[0].startswith("Chunk ID:")
        or lines[0].startswith("Wall time:")
        or lines[0].startswith("Process exited with code ")
        or lines[0].startswith("Process running with session ID ")
        or lines[0].startswith("Exit code:")
        or lines[0].startswith("Original token count:")
        or lines[0].startswith("Final output:")
        or lines[0].startswith("Original output:")
        or lines[0].startswith("Output:")
    ):
        lines.pop(0)
    return " ".join("\n".join(lines).split())


def transcript_extractor(record: dict, tool_summary) -> tuple[str, object] | None:
    """Match Codex's rollout JSONL: {"type": "response_item", "payload": ...}."""
    if str(record.get("type") or "").strip().lower() != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    pt = str(payload.get("type") or "").strip().lower()
    if pt == "message":
        role = str(payload.get("role") or "").strip().lower()
        if role in ("user", "assistant"):
            return role, payload.get("content")
        return None
    if pt in ("function_call", "custom_tool_call", "tool_call"):
        name = str(payload.get("name") or "tool")
        args = payload.get("arguments", payload.get("input", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"input": args}
        if not isinstance(args, dict):
            args = {"input": args}
        return "tool", tool_summary(name, args)
    if pt in ("function_call_output", "custom_tool_call_output",
              "tool_call_output"):
        output = tool_result_text(payload.get("output"))
        return "output", ("↳ " + output[:400]) if output else None
    return None


def build_integration(config: dict) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.codex): home."""
    home = (os.path.expanduser(str(config.get("home") or ""))
            or default_home())
    app_server_enabled = bool(config.get("app_server_usage", True))
    agent = CodexAgent(home, app_server_enabled=app_server_enabled)
    hooks_file = hooks_path(home)

    def _install_hooks(*, script_path, hook_wait_ceiling, remote_permissions,
                       remote_questions, permission_timeout, log=None):
        install_hooks(
            hooks_file,
            script_path,
            hook_wait_ceiling=hook_wait_ceiling,
            remote_permissions=remote_permissions,
            remote_questions=remote_questions,
            permission_timeout=permission_timeout,
            vprint=log,
        )

    return base.AgentIntegration(
        spec=SPEC,
        agent=agent,
        hook_modes=HOOK_MODES,
        usage_fetcher=agent.get_usage,
        session_reset_consumer=agent.consume_session_reset,
        install_hooks=_install_hooks,
        removable_hook_paths=(hooks_file,),
        transcript_path_for_session=agent.rollout_path_for_session,
        transcript_extractor=transcript_extractor,
    )


class CodexAgent:
    def __init__(
        self,
        code_home: str,
        *,
        app_server_enabled: bool = True,
        rpc: Callable[[list[dict]], list[dict]] | None = None,
    ) -> None:
        self.code_home = code_home
        self.app_server_enabled = app_server_enabled
        self.rpc = rpc

    def rollout_path_for_session(self, session_id: str) -> str:
        return rollout_path_for_session(self.code_home, session_id)

    def latest_rollout_path(self) -> str:
        return latest_rollout_path(self.code_home)

    def usage_from_rollout(self, path: str) -> dict | None:
        return usage_from_rollout(path)

    def get_usage(self) -> dict | None:
        if self.app_server_enabled:
            usage = get_app_server_usage(self.code_home, rpc=self.rpc)
            if usage is not None:
                return usage
        return get_usage(self.code_home)

    def consume_session_reset(self) -> dict:
        return consume_session_reset(self.code_home, rpc=self.rpc)


def app_server_rpc(
    code_home: str,
    requests: list[dict],
    *,
    timeout: float = 8.0,
) -> list[dict]:
    """Run Codex app-server for a small JSON-RPC exchange.

    The app-server owns Codex auth/token refresh. We initialize a short-lived
    stdio transport, send the requested account RPCs, and return matching
    responses by id. Notifications are ignored.
    """
    env = os.environ.copy()
    env["CODEX_HOME"] = code_home
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def read_responses(wanted: list[object], deadline: float) -> dict[object, dict]:
        wanted_set = set(wanted)
        responses: dict[object, dict] = {}
        while wanted_set - set(responses):
            if time.monotonic() > deadline:
                raise TimeoutError("codex app-server timed out")
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except Exception:
                continue
            mid = message.get("id")
            if mid in wanted_set:
                responses[mid] = message
        return responses

    try:
        import time
        deadline = time.monotonic() + timeout
        send({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "codelight",
                    "title": "codelight",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        })
        init_response = read_responses([1], deadline).get(1, {})
        if init_response.get("error"):
            raise RuntimeError(f"codex app-server initialize failed: {init_response['error']}")
        send({"method": "initialized", "params": {}})
        for request in requests:
            send(request)

        wanted = [request.get("id") for request in requests if request.get("id") is not None]
        responses = read_responses(wanted, deadline)
        return [responses[i] for i in wanted if i in responses]
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


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


# Codex reports two rate-limit windows positionally (primary/secondary), but
# which slot holds the ~5h session limit vs the ~7d weekly cap shifts with plan
# and policy — since 2026-07 a weekly-only plan reports the weekly window in
# `primary` and leaves `secondary` null, which otherwise renders as a
# session/weekly swap in the clients. Classify each window by its length rather
# than its slot; fall back to position for windows that omit their length (e.g.
# the app-server RPC, whose payload carries no window-minutes field).
WEEKLY_WINDOW_MIN_MINUTES = 1440  # >= 1 day → weekly cap; shorter → session window
# Codex spells the window length differently across payloads — `window_minutes`
# in the rollout JSONL, `windowDurationMins` in the app-server RPC. Accept every
# spelling we've seen so classification never silently falls back to position.
WINDOW_MINUTES_KEYS = ("window_minutes", "windowDurationMins", "windowMinutes")


def _window_minutes(window: dict) -> float:
    for key in WINDOW_MINUTES_KEYS:
        try:
            minutes = float(window.get(key) or 0.0)
        except (TypeError, ValueError):
            minutes = 0.0
        if minutes:
            return minutes
    return 0.0


def classify_rate_limit_windows(primary, secondary):
    """Bucket Codex's two rate-limit windows into (session, weekly) by length."""
    primary = primary if isinstance(primary, dict) else {}
    secondary = secondary if isinstance(secondary, dict) else {}
    session: dict = {}
    weekly: dict = {}
    for window in (primary, secondary):
        if not window:
            continue
        minutes = _window_minutes(window)
        if minutes >= WEEKLY_WINDOW_MIN_MINUTES:
            weekly = weekly or window
        elif minutes > 0:
            session = session or window
        elif not session:  # unknown length → positional fallback (primary=session)
            session = window
        elif not weekly:
            weekly = window
    return session, weekly


def _meter_fields(session, weekly, *, pct, reset_at) -> dict:
    """Emit meter keys only for windows Codex actually reports, so clients hide
    (rather than zero) a limit the plan no longer has — e.g. the 5-hour session
    window after OpenAI's 2026-07 weekly-only change. The keys reappear on their
    own once the window comes back."""
    fields: dict = {}
    if weekly:
        weekly_reset_at = reset_at(weekly)
        fields["weekly_pct"] = pct(weekly)
        fields["weekly_reset"] = format_epoch_countdown(weekly_reset_at)
        fields["weekly_reset_at"] = weekly_reset_at
    if session:
        session_reset_at = reset_at(session)
        fields["session_pct"] = pct(session)
        fields["session_reset"] = format_epoch_countdown(session_reset_at)
        fields["session_reset_at"] = session_reset_at
    return fields


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

    session, weekly = classify_rate_limit_windows(
        limits.get("primary"), limits.get("secondary"))

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

    return _meter_fields(session, weekly, pct=pct, reset_at=reset_at)


def pct_from_window(window: dict) -> float:
    try:
        return max(0.0, min(1.0, float(window.get("usedPercent") or 0.0) / 100.0))
    except (TypeError, ValueError):
        return 0.0


def reset_at_from_window(window: dict) -> int:
    try:
        return int(window.get("resetsAt") or 0)
    except (TypeError, ValueError):
        return 0


def usage_from_app_server_rate_limits(result: dict) -> dict | None:
    rate_limits = result.get("rateLimitsByLimitId")
    if isinstance(rate_limits, dict):
        codex_limits = rate_limits.get("codex")
    else:
        codex_limits = result.get("rateLimits")
    if not isinstance(codex_limits, dict):
        return None

    session, weekly = classify_rate_limit_windows(
        codex_limits.get("primary"), codex_limits.get("secondary"))

    usage = _meter_fields(session, weekly,
                          pct=pct_from_window, reset_at=reset_at_from_window)

    reset_credits = result.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        usage["rateLimitResetCredits"] = reset_credits
        usage["rate_limit_reset_available_count"] = int(
            reset_credits.get("availableCount") or 0)
    return usage


def get_app_server_usage(
    code_home: str,
    *,
    rpc: Callable[[list[dict]], list[dict]] | None = None,
) -> dict | None:
    call = rpc or (lambda requests: app_server_rpc(code_home, requests))
    try:
        responses = call([{"method": "account/rateLimits/read", "id": 2}])
        response = responses[0] if responses else {}
        result = response.get("result")
        if isinstance(result, dict):
            return usage_from_app_server_rate_limits(result)
        if response.get("error"):
            print(f"[usage] codex app-server error: {response['error']}",
                  file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[usage] codex app-server unavailable: {e}",
              file=sys.stderr, flush=True)
    return None


def consume_session_reset(
    code_home: str,
    *,
    rpc: Callable[[list[dict]], list[dict]] | None = None,
) -> dict:
    call = rpc or (lambda requests: app_server_rpc(code_home, requests))
    idempotency_key = str(uuid.uuid4())
    responses = call([
        {
            "method": "account/rateLimitResetCredit/consume",
            "id": 2,
            "params": {"idempotencyKey": idempotency_key},
        },
        {"method": "account/rateLimits/read", "id": 3},
    ])
    by_id = {response.get("id"): response for response in responses}
    consume_response = by_id.get(2, {})
    read_response = by_id.get(3, {})
    if consume_response.get("error"):
        return {
            "ok": False,
            "outcome": "error",
            "message": str(consume_response["error"]),
        }
    outcome = "unknown"
    result = consume_response.get("result")
    if isinstance(result, dict):
        outcome = str(result.get("outcome") or "unknown")
    read_result = read_response.get("result")
    usage = usage_from_app_server_rate_limits(read_result) \
        if isinstance(read_result, dict) else None
    return {
        "ok": outcome in ("reset", "alreadyRedeemed"),
        "outcome": outcome,
        "usage": usage,
    }


def get_usage(code_home: str) -> dict | None:
    """Return rate-limit data from the most recent rollout that contains it.

    A session that was cut off (e.g. hit the 5-hour cap on the first turn) may
    produce a rollout file with no token_count events.  Fall back through up to
    five recent files so a single empty session doesn't hide the last known limit.
    """
    try:
        base = os.path.join(code_home, "sessions")
        candidates: list[tuple[float, str]] = []
        for root, _, files in os.walk(base):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                candidates.append((mtime, path))
        for _, path in sorted(candidates, reverse=True)[:5]:
            result = usage_from_rollout(path)
            if result is not None:
                return result
    except Exception:
        pass
    return None


def hooks_path(code_home: str) -> str:
    return os.path.join(code_home, "hooks.json")


def install_hooks(
    hooks_file: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    remote_questions: bool = False,
    permission_timeout: int = 60,
    vprint: Callable[[str], None] | None = None,
) -> None:
    cmd_base = hooks_core.hook_command_base(script_path, "codex")
    if remote_permissions:
        perm_hook = hooks_core.command_hook(
            f"{cmd_base} permission --permission-timeout {permission_timeout}",
            timeout=hook_wait_ceiling + 15,
            status_message="Waiting for codelight approval")
    else:
        perm_hook = hooks_core.command_hook(f"{cmd_base} waiting")

    desired: list[hooks_core.HookSpec] = [
        ("SessionStart",     "startup|resume|clear|compact",
         hooks_core.command_hook(f"{cmd_base} working")),
        ("UserPromptSubmit", "", hooks_core.command_hook(f"{cmd_base} working")),
        ("PreToolUse",      "", hooks_core.command_hook(f"{cmd_base} working")),
        ("PostToolUse",     "", hooks_core.command_hook(f"{cmd_base} working")),
        ("PermissionRequest", "", perm_hook),
        ("Stop",            "", hooks_core.command_hook(f"{cmd_base} ended")),
        ("SubagentStart",    "", hooks_core.command_hook(f"{cmd_base} working")),
        ("SubagentStop",     "", hooks_core.command_hook(f"{cmd_base} working")),
    ]
    if remote_questions:
        desired.append(("PreToolUse", "^request_user_input$", hooks_core.command_hook(
            f"{cmd_base} question-codex --permission-timeout {permission_timeout}",
            timeout=hook_wait_ceiling + 15,
            status_message="Waiting for codelight answer")))

    hooks_core.install_matcher_group_hooks(
        hooks_file, desired, "codex-hooks", vprint=vprint)
    print("[codex-hooks] review new or changed hooks with /hooks in Codex CLI",
          flush=True)
