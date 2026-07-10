from __future__ import annotations

import json
import os
from typing import Callable

from codelight_core import hooks as hooks_core
from codelight_core.agents import base
from codelight_core.timefmt import format_epoch_countdown


SPEC = base.AgentSpec(
    "codex",
    "Codex",
    executables=("codex",),
    vscode_extensions=frozenset({"openai.chatgpt"}),
)

HOOK_MODES = (
    base.HookMode("question-codex", kind="question",
                  envelope=base.CONTEXT, default_agent_id="codex"),
)


def default_home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def build_integration(agent: CodexAgent, *, home: str) -> base.AgentIntegration:
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
        hook_modes=HOOK_MODES,
        usage_fetcher=agent.get_usage,
        install_hooks=_install_hooks,
        removable_hook_paths=(hooks_file,),
        transcript_path_for_session=agent.rollout_path_for_session,
    )


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
