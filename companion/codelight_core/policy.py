from __future__ import annotations

import json
import os
import sys
import threading
import time


def norm_path(path: str) -> str:
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(path or ""))))
    except Exception:
        return ""


def load_policy(policy_path: str) -> dict:
    try:
        with open(policy_path) as stream:
            value = json.load(stream)
        if isinstance(value, dict) and value.get("version") == 1:
            return value
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[policy] could not read {policy_path}: {e}",
              file=sys.stderr, flush=True)
    return {"version": 1, "trusted_folders": [], "allowed_commands": [],
            "allowed_tools": []}


def write_policy(policy_path: str, policy: dict) -> bool:
    """Atomically persist the user-owned cross-agent permission policy."""
    tmp = f"{policy_path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(policy_path), mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(policy, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, policy_path)
        return True
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"[policy] could not write {policy_path}: {e}",
              file=sys.stderr, flush=True)
        return False


def trusted_folders(policy_path: str) -> list[str]:
    """Return normalized trusted roots from codelight's policy."""
    out: list[str] = []
    seen: set[str] = set()
    raw = load_policy(policy_path).get("trusted_folders", [])
    candidates = raw if isinstance(raw, list) else []
    for item in candidates:
        if not isinstance(item, str):
            continue
        path = norm_path(item)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except Exception:
        return False


def is_trusted_repo_cwd(policy_path: str, cwd: str) -> bool:
    p = norm_path(cwd)
    if not p:
        return False
    for root in trusted_folders(policy_path):
        if path_is_within(p, root):
            return True
    return False


def repo_root_for(cwd: str) -> str:
    cur = norm_path(cwd)
    if not cur:
        return ""
    while True:
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return norm_path(cwd)
        cur = parent


def allow_folder(policy_path: str, lock: threading.Lock, cwd: str) -> tuple[bool, str]:
    folder = repo_root_for(cwd)
    if not folder:
        return False, ""

    with lock:
        policy = load_policy(policy_path)
        raw = policy.get("trusted_folders", [])
        allowed = [norm_path(x) for x in raw if isinstance(x, str)] \
            if isinstance(raw, list) else []
        for existing in allowed:
            if existing and path_is_within(folder, existing):
                return True, existing
        policy["trusted_folders"] = [*filter(None, allowed), folder]
        policy.setdefault("allowed_commands", [])
        return write_policy(policy_path, policy), folder


def command_from_tool(tool_name: str, tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    name = str(tool_name or "").strip()
    key = "command" if name in {"Bash", "run_in_terminal"} else \
        "cmd" if name == "exec_command" else ""
    command = tool_input.get(key) if key else None
    if not isinstance(command, str):
        return ""
    command = command.strip()
    return command if 0 < len(command) <= 4096 else ""


def is_allowed_command(policy_path: str, tool_name: str, tool_input, cwd: str) -> bool:
    command = command_from_tool(tool_name, tool_input)
    current = norm_path(cwd)
    if not command or not current:
        return False
    raw = load_policy(policy_path).get("allowed_commands", [])
    if not isinstance(raw, list):
        return False
    for item in raw:
        if not isinstance(item, dict) or item.get("command") != command:
            continue
        root = norm_path(item.get("folder", ""))
        if root and path_is_within(current, root):
            return True
    return False


def allow_command(policy_path: str, lock: threading.Lock,
                  command: str, cwd: str) -> tuple[bool, str]:
    command = str(command or "").strip()
    folder = repo_root_for(cwd)
    if not command or len(command) > 4096 or not folder:
        return False, ""
    with lock:
        policy = load_policy(policy_path)
        raw = policy.get("allowed_commands", [])
        allowed = [x for x in raw if isinstance(x, dict)] \
            if isinstance(raw, list) else []
        if any(x.get("command") == command
               and norm_path(x.get("folder", "")) == folder for x in allowed):
            return True, command
        allowed.append({"command": command, "folder": folder})
        policy["allowed_commands"] = allowed
        policy.setdefault("trusted_folders", [])
        return write_policy(policy_path, policy), command


def is_allowed_tool(policy_path: str, tool_name: str) -> bool:
    """Whether the user has always-allowed this tool ("allow forever")."""
    tool = str(tool_name or "").strip()
    if not tool or tool == "?":
        return False
    raw = load_policy(policy_path).get("allowed_tools", [])
    if not isinstance(raw, list):
        return False
    return any(isinstance(item, dict) and item.get("tool") == tool
               for item in raw)


def allow_tool(policy_path: str, lock: threading.Lock,
               tool_name: str) -> tuple[bool, str]:
    """Always-allow a tool by name, recording added_at/last_used for later
    review and cleanup."""
    tool = str(tool_name or "").strip()
    if not tool or tool == "?" or len(tool) > 200:
        return False, ""
    now = int(time.time())
    with lock:
        policy = load_policy(policy_path)
        raw = policy.get("allowed_tools", [])
        allowed = [x for x in raw if isinstance(x, dict)] \
            if isinstance(raw, list) else []
        for item in allowed:
            if item.get("tool") == tool:
                item["last_used"] = now
                policy["allowed_tools"] = allowed
                return write_policy(policy_path, policy), tool
        allowed.append({"tool": tool, "added_at": now, "last_used": now})
        policy["allowed_tools"] = allowed
        policy.setdefault("trusted_folders", [])
        policy.setdefault("allowed_commands", [])
        return write_policy(policy_path, policy), tool


# Refresh last_used at most this often — the timestamp is for "is this rule
# still earning its keep" review, not an audit log; no point rewriting the
# policy file on every hook invocation.
TOUCH_INTERVAL_SECS = 3600


def touch_allowed_tool(policy_path: str, lock: threading.Lock,
                       tool_name: str) -> None:
    tool = str(tool_name or "").strip()
    if not tool:
        return
    now = int(time.time())
    with lock:
        policy = load_policy(policy_path)
        raw = policy.get("allowed_tools", [])
        if not isinstance(raw, list):
            return
        for item in raw:
            if isinstance(item, dict) and item.get("tool") == tool:
                last = item.get("last_used")
                if isinstance(last, int) and now - last < TOUCH_INTERVAL_SECS:
                    return
                item["last_used"] = now
                write_policy(policy_path, policy)
                return


def truncate_tool_input(tool_input, max_str: int = 500, max_total: int = 3000):
    """Bound tool_input for transport: long strings clipped, payload capped."""
    def clip(v, depth=0):
        if isinstance(v, str):
            return v if len(v) <= max_str else v[:max_str] + "…"
        if isinstance(v, dict) and depth < 4:
            return {k: clip(x, depth + 1) for k, x in list(v.items())[:20]}
        if isinstance(v, list) and depth < 4:
            return [clip(x, depth + 1) for x in v[:10]]
        return v

    out = clip(tool_input)
    try:
        if len(json.dumps(out)) > max_total:
            return {"_truncated": json.dumps(out)[:max_total] + "…"}
    except Exception:
        return {}
    return out


def tool_summary(tool_name: str, tool_input: dict) -> str:
    """One-line human summary of what an agent wants to do."""
    def compact(s: str) -> str:
        return " ".join(str(s).split())

    def first_patch_file(patch_text: str) -> str:
        for line in str(patch_text).splitlines():
            line = line.strip()
            if line.startswith("*** Update File:"):
                return line.split(":", 1)[1].strip()
            if line.startswith("*** Add File:"):
                return line.split(":", 1)[1].strip()
            if line.startswith("*** Delete File:"):
                return line.split(":", 1)[1].strip()
        return ""

    if tool_name in ("Bash", "exec_command"):
        detail = tool_input.get("command", "") or tool_input.get("cmd", "")
    elif tool_name in ("Edit", "Write", "Read", "NotebookEdit"):
        detail = tool_input.get("file_path", "")
    elif tool_name in ("WebFetch", "WebSearch"):
        detail = tool_input.get("url", "") or tool_input.get("query", "")
    elif tool_name == "apply_patch":
        explanation = compact(tool_input.get("explanation", ""))
        first_file = first_patch_file(tool_input.get("input", ""))
        parts = []
        if explanation:
            parts.append(explanation)
        if first_file:
            parts.append(f"target={first_file}")
        detail = " | ".join(parts)
    elif tool_name == "run_in_terminal":
        detail = tool_input.get("goal", "") or tool_input.get("explanation", "")
    elif tool_name == "write_stdin":
        detail = "read running command output"
    elif tool_name == "update_plan":
        detail = tool_input.get("explanation", "") or "update task progress"
    elif tool_name == "request_user_input":
        detail = "ask for input"
    elif tool_name in ("view_image", "open_image"):
        detail = tool_input.get("path", "")
    elif tool_name in ("tool_search_tool", "tool_search"):
        detail = tool_input.get("query", "")
    elif tool_name in ("create_file", "read_file"):
        detail = tool_input.get("filePath", "") or tool_input.get("file_path", "")
    elif tool_name in ("get_errors", "grep_search", "file_search"):
        detail = tool_input.get("query", "") or tool_input.get("includePattern", "")
    else:
        try:
            detail = json.dumps(tool_input)
        except Exception:
            detail = ""
    detail = compact(detail)
    if len(detail) > 200:
        detail = detail[:200] + "…"
    return f"{tool_name}: {detail}" if detail else tool_name


def is_safe_memory_read(tool_name: str, tool_input) -> bool:
    """Allow memory reads for repo/session scopes only."""
    if str(tool_name or "").strip() != "memory":
        return False
    if not isinstance(tool_input, dict):
        return False

    command = str(tool_input.get("command") or "").strip().lower()
    path = str(tool_input.get("path") or "").strip()
    if command != "view" or not path:
        return False

    return (
        path.startswith("/memories/repo/")
        or path == "/memories/repo"
        or path.startswith("/memories/session/")
        or path == "/memories/session"
    )


def extract_patch_targets(patch_text: str) -> tuple[list[str], bool]:
    """Return patch target paths and whether it includes a file delete action."""
    targets: list[str] = []
    has_delete = False
    for raw in str(patch_text or "").splitlines():
        line = raw.strip()
        if line.startswith("*** Update File:"):
            targets.append(line.split(":", 1)[1].strip())
        elif line.startswith("*** Add File:"):
            targets.append(line.split(":", 1)[1].strip())
        elif line.startswith("*** Delete File:"):
            has_delete = True
    return targets, has_delete


def is_trusted_target_path(policy_path: str, path: str, cwd: str) -> bool:
    p = str(path or "").strip()
    if not p:
        return False
    if os.path.isabs(p):
        candidate = norm_path(p)
    else:
        candidate = norm_path(os.path.join(cwd or "", p))
    if not candidate:
        return False
    for root in trusted_folders(policy_path):
        if path_is_within(candidate, root):
            return True
    return False


def is_safe_trusted_apply_patch(policy_path: str, tool_name: str,
                                tool_input, cwd: str) -> bool:
    """Allow apply_patch automatically only for in-trusted-folder edits."""
    if str(tool_name or "").strip() != "apply_patch":
        return False
    if not isinstance(tool_input, dict):
        return False

    targets, has_delete = extract_patch_targets(tool_input.get("input", ""))
    if has_delete or not targets:
        return False
    return all(is_trusted_target_path(policy_path, target, cwd)
               for target in targets)
