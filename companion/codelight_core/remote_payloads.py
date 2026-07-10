from __future__ import annotations

from typing import Callable


AgentName = Callable[[str | None], str]


def permission_request_payload(
    entry: dict,
    *,
    agent_display_name: AgentName,
    allow_folder_available: bool,
    allow_command_available: bool,
) -> dict:
    cwd = str(entry.get("cwd") or "")
    agent_id = str(entry.get("agent_id") or "")
    return {
        "type": "permission_request",
        "id": entry["id"],
        "tool_name": entry["tool_name"],
        "summary": entry["summary"],
        "tool_input": entry["tool_input"],
        "session_id": entry["session_id"],
        "agent_id": agent_id,
        "agent_display": agent_display_name(agent_id),
        "cwd": cwd,
        "allow_folder_available": allow_folder_available,
        "allow_command_available": allow_command_available,
        "expires_at": int(entry["expires"]),
    }


def permission_resolved_payload(
    entry: dict,
    *,
    decision: str,
    by: str,
    persistence: dict | None,
    agent_display_name: AgentName,
) -> dict:
    agent_id = str(entry.get("agent_id") or "")
    persistence = persistence or {}
    return {
        "type": "permission_resolved",
        "id": entry["id"],
        "decision": decision,
        "by": by or "",
        "agent_id": agent_id,
        "agent_display": agent_display_name(agent_id),
        "policy_kind": persistence.get("kind", ""),
        "policy_value": persistence.get("value", ""),
        "policy_persisted": bool(persistence.get("persisted")),
    }


def question_request_payload(entry: dict, *,
                             agent_display_name: AgentName) -> dict:
    agent_id = str(entry.get("agent_id") or "")
    return {
        "type": "question_request",
        "id": entry["id"],
        "questions": entry["questions"],
        "session_id": entry["session_id"],
        "agent_id": agent_id,
        "agent_display": agent_display_name(agent_id),
        "cwd": entry["cwd"],
        "expires_at": int(entry["expires"]),
    }


def question_resolved_payload(entry: dict, *, by: str,
                              agent_display_name: AgentName) -> dict:
    agent_id = str(entry.get("agent_id") or "")
    return {
        "type": "question_resolved",
        "id": entry["id"],
        "by": by or "",
        "agent_id": agent_id,
        "agent_display": agent_display_name(agent_id),
    }
