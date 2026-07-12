from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections.abc import Callable

from codelight_core import hook_io
from codelight_core import hook_runtime
from codelight_core import policy as policy_core
from codelight_core import transcript as transcript_core
from codelight_core.agents import base as agents_base


AgentNameCallback = Callable[[str | None], str]
AgentDisplayCallback = Callable[[str | None], str]


def resolve_hook_agent(agent_id: str) -> tuple[str, str]:
    """Resolve the agent that actually ran this hook, and its session id.

    Grok runs other harnesses' hooks via its compatibility layer (Claude
    Code / Cursor), which would otherwise be misattributed to `--agent
    claude`/`cursor`. Grok sets GROK_* env on every hook it runs, so detect it
    and re-tag to grok (with its own GROK_SESSION_ID). Returns (agent_id,
    session_id_override) where the override is "" when the payload's own
    session id should be used.
    """
    grok_session = os.environ.get("GROK_SESSION_ID", "")
    if grok_session or os.environ.get("GROK_HOOK_EVENT"):
        return "grok", grok_session
    return agent_id, ""


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
    agent_id, session_override = resolve_hook_agent(agent_id)
    session_id = session_override or hook_runtime.session_id(data)
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
            # Cursor sends workspace_roots instead of a top-level cwd.
            "cwd": str(data.get("cwd")
                       or (data.get("workspace_roots") or [""])[0] or ""),
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
    envelope: str,
    reason: str = "",
) -> None:
    """Emit the host-specific decision envelope from one shared policy path."""
    print(json.dumps(hook_runtime.permission_decision_output(
        decision,
        envelope=envelope,
        reason=reason,
    )))


def run_permission_hook(
    *,
    mode: agents_base.HookMode,
    agent_id: str | None = None,
    socket_path: str,
    monitor_state_dir: str,
    policy_path: str,
    policy_lock: threading.Lock,
    hook_wait_ceiling: int,
    normalize_agent_id: AgentNameCallback,
    agent_display_name: AgentDisplayCallback,
    auto_allow_tools: Callable[[str], frozenset[str]] = lambda agent_id: frozenset(),
    input_text: str | None = None,
) -> None:
    """Forward a permission prompt to the daemon or fall back to local prompt."""
    data = hook_runtime.parse_json_object(
        sys.stdin.read() if input_text is None else input_text)
    resolved_agent, session_override = resolve_hook_agent(
        agent_id or mode.default_agent_id)
    session_id = session_override or hook_runtime.session_id(data)
    normalized_agent = normalize_agent_id(resolved_agent)
    tool_name = hook_runtime.tool_name(data)
    tool_input = hook_runtime.tool_input(data)
    cwd = str(data.get("cwd") or "")

    # Cursor payload shapes: beforeShellExecution carries the command at the
    # top level (no tool_name), and beforeMCPExecution serializes tool_input
    # as a JSON string.
    if tool_name == "?" and isinstance(data.get("command"), str):
        tool_name = "Bash"
        tool_input = {"command": data["command"]}
    if not tool_input and isinstance(data.get("tool_input"), str):
        parsed = hook_runtime.parse_json_object(data["tool_input"])
        if parsed:
            tool_input = parsed

    if mode.requires_tool_use_id and not data.get("tool_use_id"):
        return

    if hook_runtime.is_question_tool(tool_name, tool_input):
        return

    if policy_core.is_safe_memory_read(tool_name, tool_input):
        emit_permission_decision(
            "allow",
            envelope=mode.envelope,
            reason="Read-only memory view in repo/session scope")
        return

    if policy_core.is_allowed_command(policy_path, tool_name, tool_input, cwd):
        emit_permission_decision(
            "allow",
            envelope=mode.envelope,
            reason="Exact command allowed by codelight policy")
        return

    if policy_core.is_allowed_tool(policy_path, tool_name):
        policy_core.touch_allowed_tool(policy_path, policy_lock, tool_name)
        emit_permission_decision(
            "allow",
            envelope=mode.envelope,
            reason="Tool always allowed by codelight policy")
        return

    if policy_core.is_safe_trusted_apply_patch(
        policy_path, tool_name, tool_input, cwd):
        emit_permission_decision(
            "allow",
            envelope=mode.envelope,
            reason="apply_patch target is within trusted codelight folder")
        return

    if (
        policy_core.is_trusted_repo_cwd(policy_path, cwd)
        and tool_name in auto_allow_tools(normalized_agent)
    ):
        emit_permission_decision(
            "allow",
            envelope=mode.envelope,
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

    decision = None
    try:
        response = hook_io.request_json(
            socket_path,
            request,
            connect_timeout=2.0,
            response_timeout=hook_wait_ceiling,
            max_bytes=4096,
        )
        decision = response.get("decision") if response else None
    except Exception:
        try:
            hook_io.write_monitor_state(
                monitor_state_dir,
                session_id=session_id,
                state="waiting",
                agent_id=normalized_agent,
            )
        except Exception:
            pass

    if decision in ("allow", "deny"):
        emit_permission_decision(
            decision,
            envelope=mode.envelope,
            reason="Denied by remote codelight approval" if decision == "deny" else "")
    elif mode.fallback_decision:
        # No remote decision — hand back to the agent's own prompt explicitly
        # (e.g. Cursor's {"permission": "ask"}).
        emit_permission_decision(mode.fallback_decision, envelope=mode.envelope)


def run_question_hook(
    *,
    mode: agents_base.HookMode,
    agent_id: str | None = None,
    socket_path: str,
    hook_wait_ceiling: int,
    normalize_agent_id: AgentNameCallback,
    agent_display_name: AgentDisplayCallback,
    input_text: str | None = None,
) -> None:
    """Forward AskUserQuestion prompts to the daemon and emit hook output."""
    resolved_agent, session_override = resolve_hook_agent(
        agent_id or mode.default_agent_id)
    normalized_agent = normalize_agent_id(resolved_agent)

    data = hook_runtime.parse_json_object(
        sys.stdin.read() if input_text is None else input_text)
    tool_input = hook_runtime.tool_input(data)
    questions = hook_runtime.questions_from_input(data, tool_input)
    if not questions:
        return

    request = {
        "type":       "question_request",
        "session_id": session_override or hook_runtime.session_id(data),
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
            if mode.envelope == agents_base.CONTEXT:
                print(json.dumps(hook_runtime.question_context_output(answers)))
            else:
                print(json.dumps(
                    hook_runtime.question_updated_input_output(tool_input, answers)
                ))
    except Exception:
        pass
