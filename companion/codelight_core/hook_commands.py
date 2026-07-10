from __future__ import annotations

import json
import sys
import threading
import uuid
from collections.abc import Callable

from codelight_core import hook_io
from codelight_core import hook_runtime
from codelight_core import policy as policy_core
from codelight_core import transcript as transcript_core


AgentNameCallback = Callable[[str | None], str]
AgentDisplayCallback = Callable[[str | None], str]


def run_status_hook(
    state: str,
    *,
    agent_id: str,
    socket_path: str,
    monitor_state_dir: str,
    normalize_agent_id: AgentNameCallback,
    input_text: str | None = None,
) -> None:
    """Send a fast status event to the daemon, falling back to monitor_state."""
    data = hook_runtime.parse_json_object(
        sys.stdin.read() if input_text is None else input_text)
    session_id = hook_runtime.session_id(data)
    transcript_path = transcript_core.extract_transcript_path(data)
    hook_event = hook_runtime.hook_event_name(data)
    normalized_agent = normalize_agent_id(agent_id)

    if hook_io.send_json(
        socket_path,
        {
            "state": state,
            "session_id": session_id,
            "agent_id": normalized_agent,
            "transcript_path": transcript_path,
            "cwd": data.get("cwd", ""),
            "hook_event": hook_event,
        },
        timeout=0.5,
    ):
        return

    hook_io.write_monitor_state(
        monitor_state_dir,
        session_id=session_id,
        state=state,
        agent_id=normalized_agent,
        hook_event=hook_event,
    )


def emit_permission_decision(
    decision: str,
    *,
    copilot_mode: bool,
    vscode_prettool_mode: bool,
    reason: str = "",
) -> None:
    """Emit the host-specific decision envelope from one shared policy path."""
    print(json.dumps(hook_runtime.permission_decision_output(
        decision,
        copilot_mode=copilot_mode,
        vscode_prettool_mode=vscode_prettool_mode,
        reason=reason,
    )))


def run_permission_hook(
    *,
    copilot_mode: bool = False,
    vscode_prettool_mode: bool = False,
    agent_id: str | None = None,
    socket_path: str,
    monitor_state_dir: str,
    policy_path: str,
    policy_lock: threading.Lock,
    hook_wait_ceiling: int,
    normalize_agent_id: AgentNameCallback,
    agent_display_name: AgentDisplayCallback,
    input_text: str | None = None,
) -> None:
    """Forward a permission prompt to the daemon or fall back to local prompt."""
    data = hook_runtime.parse_json_object(
        sys.stdin.read() if input_text is None else input_text)
    session_id = hook_runtime.session_id(data)
    normalized_agent = normalize_agent_id(
        agent_id or ("copilot" if (copilot_mode or vscode_prettool_mode) else "claude")
    )
    tool_name = hook_runtime.tool_name(data)
    tool_input = hook_runtime.tool_input(data)
    cwd = str(data.get("cwd") or "")

    if vscode_prettool_mode and not data.get("tool_use_id"):
        return

    if hook_runtime.is_question_tool(tool_name, tool_input):
        return

    if policy_core.is_safe_memory_read(tool_name, tool_input):
        emit_permission_decision(
            "allow",
            copilot_mode=copilot_mode,
            vscode_prettool_mode=vscode_prettool_mode,
            reason="Read-only memory view in repo/session scope")
        return

    if policy_core.is_allowed_command(policy_path, tool_name, tool_input, cwd):
        emit_permission_decision(
            "allow",
            copilot_mode=copilot_mode,
            vscode_prettool_mode=vscode_prettool_mode,
            reason="Exact command allowed by codelight policy")
        return

    if policy_core.is_safe_trusted_apply_patch(
        policy_path, tool_name, tool_input, cwd):
        emit_permission_decision(
            "allow",
            copilot_mode=copilot_mode,
            vscode_prettool_mode=vscode_prettool_mode,
            reason="apply_patch target is within trusted codelight folder")
        return

    if (
        policy_core.is_trusted_repo_cwd(policy_path, cwd)
        and policy_core.is_trusted_auto_allow_tool(tool_name)
    ):
        emit_permission_decision(
            "allow",
            copilot_mode=copilot_mode,
            vscode_prettool_mode=vscode_prettool_mode,
            reason="Read-only tool in trusted codelight folder")
        return

    truncated = (
        policy_core.truncate_tool_input(
            tool_input, max_str=8000, max_total=12000)
        if tool_name == "ExitPlanMode"
        else policy_core.truncate_tool_input(tool_input)
    )
    request = {
        "type":       "permission_request",
        "session_id": session_id,
        "agent_id":   normalized_agent,
        "agent_display": agent_display_name(normalized_agent),
        "prompt_id":  data.get("prompt_id") or uuid.uuid4().hex,
        "tool_name":  tool_name,
        "summary":    policy_core.tool_summary(tool_name, tool_input),
        "tool_input": truncated,
        "policy_command": policy_core.command_from_tool(tool_name, tool_input),
        "cwd":        cwd,
    }

    try:
        response = hook_io.request_json(
            socket_path,
            request,
            connect_timeout=2.0,
            response_timeout=hook_wait_ceiling,
            max_bytes=4096,
        )
        decision = response.get("decision") if response else None
        if decision in ("allow", "deny"):
            emit_permission_decision(
                decision,
                copilot_mode=copilot_mode,
                vscode_prettool_mode=vscode_prettool_mode,
                reason="Denied by remote codelight approval" if decision == "deny" else "")
        return
    except Exception:
        pass

    try:
        hook_io.write_monitor_state(
            monitor_state_dir,
            session_id=session_id,
            state="waiting",
            agent_id=normalized_agent,
        )
    except Exception:
        pass


def run_question_hook(
    *,
    vscode_prettool_mode: bool = False,
    codex_context_mode: bool = False,
    agent_id: str | None = None,
    socket_path: str,
    hook_wait_ceiling: int,
    normalize_agent_id: AgentNameCallback,
    agent_display_name: AgentDisplayCallback,
    input_text: str | None = None,
) -> None:
    """Forward AskUserQuestion prompts to the daemon and emit hook output."""
    normalized_agent = normalize_agent_id(
        agent_id or ("copilot" if vscode_prettool_mode else "claude")
    )

    data = hook_runtime.parse_json_object(
        sys.stdin.read() if input_text is None else input_text)
    tool_input = hook_runtime.tool_input(data)
    questions = hook_runtime.questions_from_input(data, tool_input)
    if not questions:
        return

    request = {
        "type":       "question_request",
        "session_id": hook_runtime.session_id(data),
        "agent_id":   normalized_agent,
        "agent_display": agent_display_name(normalized_agent),
        "prompt_id":  data.get("prompt_id") or uuid.uuid4().hex,
        "questions":  questions,
        "cwd":        data.get("cwd", ""),
    }

    try:
        response = hook_io.request_json(
            socket_path,
            request,
            connect_timeout=2.0,
            response_timeout=hook_wait_ceiling,
            max_bytes=65536,
        )
        answers = response.get("answers") if response else None
        if isinstance(answers, dict) and answers:
            if vscode_prettool_mode or codex_context_mode:
                print(json.dumps(hook_runtime.question_context_output(answers)))
            else:
                print(json.dumps(
                    hook_runtime.question_updated_input_output(tool_input, answers)
                ))
    except Exception:
        pass
