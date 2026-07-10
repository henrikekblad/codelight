from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Callable


HookSpec = tuple[str, str, dict]


def hook_command_base(script_path: str, agent_id: str) -> str:
    return f"python3 {shlex.quote(script_path)} --agent {shlex.quote(agent_id)} --hook"


def command_hook(command: str, timeout_key: str = "timeout",
                 timeout: int | None = None,
                 status_message: str | None = None) -> dict:
    hook = {"type": "command", "command": command}
    if timeout is not None:
        hook[timeout_key] = timeout
    if status_message:
        hook["statusMessage"] = status_message
    return hook


def is_codelight_hook_cmd(cmd: str) -> bool:
    # Broader than the current command line so old installs are cleaned too.
    return (("codelight" in cmd or "claude_monitor" in cmd) and "--hook" in cmd) \
        or "monitor_hook.py" in cmd


def read_json_object(path: str, label: str) -> dict | None:
    data: dict = {}
    try:
        with open(path) as f:
            settings = json.load(f)
        if isinstance(settings, dict):
            data = settings
        else:
            print(f"[{label}] warning: {path} is not a JSON object", file=sys.stderr)
            return None
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[{label}] warning: could not read {path}: {e}", file=sys.stderr)
        return None
    return data


def write_json_object(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_matcher_group_hooks(hooks: dict, desired: list[HookSpec]) -> bool:
    before = json.dumps(hooks, sort_keys=True)

    for event in list(hooks.keys()):
        event_hooks = hooks.get(event, [])
        if not isinstance(event_hooks, list):
            continue
        cleaned = []
        for entry in event_hooks:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue
            entry_hooks = entry.get("hooks", [])
            if not isinstance(entry_hooks, list):
                cleaned.append(entry)
                continue
            inner = [c for c in entry_hooks
                     if not (isinstance(c, dict)
                             and is_codelight_hook_cmd(c.get("command", "")))]
            if inner:
                cleaned.append({**entry, "hooks": inner})
            elif not entry.get("hooks"):
                cleaned.append(entry)
        if cleaned:
            hooks[event] = cleaned
        else:
            del hooks[event]

    for event, matcher, hook_dict in desired:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        slot = next((e for e in entries
                     if isinstance(e, dict) and e.get("matcher", "") == matcher), None)
        if slot is None:
            entries.append({"matcher": matcher, "hooks": [hook_dict]})
        else:
            slot.setdefault("hooks", []).append(hook_dict)

    return json.dumps(hooks, sort_keys=True) != before


def install_matcher_group_hooks(path: str, desired: list[HookSpec],
                                label: str, *,
                                vprint: Callable[[str], None] | None = None) -> None:
    doc = read_json_object(path, label)
    if doc is None:
        return

    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict):
        print(f"[{label}] warning: {path} has non-object hooks", file=sys.stderr)
        return

    if not merge_matcher_group_hooks(hooks, desired):
        if vprint:
            vprint(f"[{label}] already up to date")
        return

    doc["hooks"] = hooks
    write_json_object(path, doc)
    print(f"[{label}] installed in {path}", flush=True)


def remove_matcher_group_hooks(path: str) -> None:
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        print(f"[uninstall] no {os.path.basename(path)} found at {path}")
        return
    except Exception as e:
        print(f"[uninstall] could not update {path}: {e}", file=sys.stderr)
        return

    hooks = doc.get("hooks", {}) if isinstance(doc, dict) else {}
    if not isinstance(hooks, dict):
        print(f"[uninstall] no codelight hooks found in {path}")
        return

    changed = merge_matcher_group_hooks(hooks, [])
    if changed:
        doc["hooks"] = hooks
        write_json_object(path, doc)
        print(f"[uninstall] removed hooks from {path}")
    else:
        print(f"[uninstall] no codelight hooks found in {path}")


def install_claude_hooks(
    settings_path: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    remote_questions: bool = False,
    permission_timeout: int = 60,
    vprint: Callable[[str], None] | None = None,
) -> None:
    from codelight_core.agents import claude

    claude.install_hooks(
        settings_path,
        script_path,
        hook_wait_ceiling=hook_wait_ceiling,
        remote_permissions=remote_permissions,
        remote_questions=remote_questions,
        permission_timeout=permission_timeout,
        vprint=vprint,
    )


def copilot_hooks_path(copilot_home: str) -> str:
    from codelight_core.agents import copilot

    return copilot.hooks_path(copilot_home)


def codex_hooks_path(codex_home: str) -> str:
    from codelight_core.agents import codex

    return codex.hooks_path(codex_home)


def install_codex_hooks(
    hooks_path: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    remote_questions: bool = False,
    permission_timeout: int = 60,
    vprint: Callable[[str], None] | None = None,
) -> None:
    from codelight_core.agents import codex

    codex.install_hooks(
        hooks_path,
        script_path,
        hook_wait_ceiling=hook_wait_ceiling,
        remote_permissions=remote_permissions,
        remote_questions=remote_questions,
        permission_timeout=permission_timeout,
        vprint=vprint,
    )


def install_copilot_hooks(
    hooks_path: str,
    script_path: str,
    *,
    hook_wait_ceiling: int,
    remote_permissions: bool = False,
    permission_timeout: int = 60,
) -> None:
    from codelight_core.agents import copilot

    copilot.install_hooks(
        hooks_path,
        script_path,
        hook_wait_ceiling=hook_wait_ceiling,
        remote_permissions=remote_permissions,
        permission_timeout=permission_timeout,
    )
