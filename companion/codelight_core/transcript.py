from __future__ import annotations

import json
from typing import Callable


ToolSummary = Callable[[str, dict], str]


def is_noise(s: str) -> bool:
    """True for machine-generated wrappers that are not human turns."""
    return ("<command-" in s or "<system-reminder" in s or "<ide_" in s
            or "<local-command" in s or s.startswith("Caveat:"))


def tool_result_text(content) -> str:
    """Extract a short plain-text snippet from a tool_result block's content."""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        s = "\n".join(parts)
    else:
        s = ""
    return " ".join(s.split())


def codex_tool_result_text(content) -> str:
    """Remove Codex's execution envelope from a tool result."""
    if not isinstance(content, str):
        return tool_result_text(content)
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


def extract_transcript_path(data: dict) -> str:
    """Read transcript path across hook payload variants."""
    if not isinstance(data, dict):
        return ""
    for key in (
        "transcript_path",
        "transcriptPath",
        "transcript",
        "transcriptFile",
        "transcript_file",
        "log_path",
        "logPath",
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def parse_transcript(path: str, *, tool_summary: ToolSummary,
                     max_msgs: int = 60) -> list[dict]:
    """Best-effort parse of supported-agent transcript JSONL.

    Transcript formats are internal to each agent and can change without
    notice, so this parser deliberately never raises.
    """
    try:
        with open(path, "r") as f:
            raw_lines = f.readlines()
    except Exception:
        return []

    def extract_role_and_content(o: dict):
        t = str(o.get("type") or "").strip().lower()

        if t == "response_item" and isinstance(o.get("payload"), dict):
            payload = o["payload"]
            pt = str(payload.get("type") or "").strip().lower()
            if pt == "message":
                role = str(payload.get("role") or "").strip().lower()
                if role in ("user", "assistant"):
                    return role, payload.get("content")
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
                output = codex_tool_result_text(payload.get("output"))
                return "output", ("↳ " + output[:400]) if output else None

        if t in ("user.message", "assistant.message"):
            data = o.get("data")
            if isinstance(data, dict):
                content = data.get("content")
                if content is not None:
                    return ("user" if t.startswith("user") else "assistant"), content

        if t in ("user", "assistant"):
            msg = o.get("message")
            if isinstance(msg, dict):
                role = str(msg.get("role") or t)
                content = msg.get("content")
                if content is not None:
                    return role, content
            if isinstance(msg, str):
                return t, msg

        role = str(o.get("role") or "").strip().lower()
        content = o.get("content")
        if role in ("user", "assistant") and content is not None:
            return role, content

        text = o.get("text")
        if isinstance(text, str) and text.strip():
            if role in ("user", "assistant"):
                return role, text
            if "user" in t or "prompt" in t or t == "request":
                return "user", text
            if "assistant" in t or "response" in t or t == "reply":
                return "assistant", text

        prompt = o.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return "user", prompt
        response = o.get("response")
        if isinstance(response, str) and response.strip():
            return "assistant", response

        msg = o.get("message")
        if isinstance(msg, dict):
            mr = str(msg.get("role") or "").strip().lower()
            if mr in ("user", "assistant"):
                mc = msg.get("content")
                if mc is None:
                    mc = msg.get("text")
                if mc is not None:
                    return mr, mc

        return "", None

    out: list[dict] = []
    for raw in raw_lines[-8 * max_msgs:]:
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if o.get("isMeta") or o.get("isCompactSummary"):
            continue
        role, content = extract_role_and_content(o)
        if role not in ("user", "assistant", "tool", "output") or content is None:
            continue

        if isinstance(content, str):
            s = content.strip()
            if s and not is_noise(s):
                out.append({"role": role, "text": s[:2000]})
        elif isinstance(content, list):
            prose: list[str] = []
            tail: list[dict] = []
            for block in content:
                if isinstance(block, str):
                    prose.append(block)
                    continue
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt in ("text", "input_text", "output_text"):
                    prose.append(block.get("text", ""))
                elif bt == "image":
                    prose.append("[image]")
                elif bt == "tool_use":
                    tail.append({
                        "role": "tool",
                        "text": tool_summary(
                            block.get("name", "?"), block.get("input") or {}),
                    })
                elif bt == "tool_result":
                    snippet = tool_result_text(block.get("content"))
                    if snippet:
                        tail.append({"role": "output", "text": "⤷ " + snippet[:400]})
            prose_text = "\n".join(p for p in prose if p).strip()
            if prose_text and not is_noise(prose_text):
                out.append({"role": role, "text": prose_text[:2000]})
            out.extend(tail)

    return out[-max_msgs:]
