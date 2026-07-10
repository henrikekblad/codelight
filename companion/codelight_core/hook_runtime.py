from __future__ import annotations

import json

from codelight_core.agents import base as agents_base


QUESTION_TOOLS = {"AskUserQuestion", "ask_user", "askUser", "vscode_askQuestions"}


def parse_json_object(raw: str) -> dict:
    try:
        if raw.strip():
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def session_id(data: dict) -> str:
    return str(
        data.get("session_id")
        or data.get("sessionId")
        or data.get("session")
        or "unknown"
    )


def hook_event_name(data: dict) -> str:
    return str(
        data.get("hook_event_name")
        or data.get("hookEventName")
        or data.get("event_name")
        or ""
    )


def tool_input(data: dict) -> dict:
    value = data.get("tool_input")
    if value is None:
        value = data.get("toolArgs") or {}
    return value if isinstance(value, dict) else {}


def tool_name(data: dict) -> str:
    return str(data.get("tool_name") or data.get("toolName") or "?")


def is_question_tool(tool: str, value: dict) -> bool:
    has_questions = isinstance(value.get("questions"), list) and bool(value.get("questions"))
    return tool in QUESTION_TOOLS or has_questions


def questions_from_input(data: dict, value: dict) -> list:
    questions = value.get("questions")
    if not isinstance(questions, list):
        raw_questions = data.get("questions")
        questions = raw_questions if isinstance(raw_questions, list) else []
    if not questions:
        question = value.get("question") or data.get("question")
        if isinstance(question, str) and question.strip():
            questions = [{"question": question.strip()}]
    return questions


def permission_decision_output(
    decision: str,
    *,
    envelope: str = agents_base.PERMISSION_REQUEST,
    reason: str = "",
) -> dict:
    """Return the host-specific permission decision envelope."""
    if envelope == agents_base.PRETOOL_DECISION:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
            },
        }
        if reason:
            output["hookSpecificOutput"]["permissionDecisionReason"] = reason
        return output
    if envelope == agents_base.BEHAVIOR:
        output = {"behavior": decision}
        if reason and decision == "deny":
            output["message"] = reason
        return output
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        },
    }


def question_context_output(answers: dict) -> dict:
    qa_lines = [f"- {question}: {answer}" for question, answer in answers.items()]
    context = (
        "The user already answered the ask-user prompt via codelight remote UI. "
        "Do not ask the same question again; continue using these answers:\n"
        + "\n".join(qa_lines)
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Answered by codelight remote prompt",
            "additionalContext": context,
        }
    }


def question_updated_input_output(tool_input: dict, answers: dict) -> dict:
    updated = {**tool_input, "answers": answers}
    if len(answers) == 1:
        try:
            updated["answer"] = next(iter(answers.values()))
        except Exception:
            pass
    updated["responses"] = [
        {"question": question, "answer": answer}
        for question, answer in answers.items()
    ]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
            "modifiedArgs": updated,
        }
    }
