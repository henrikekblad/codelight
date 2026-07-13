"""Shared agent-integration types.

Leaf module: agent modules, the registry, and the generic hook runtime all
import it, so it must not import other codelight_core modules.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable


UsageFetcher = Callable[[], dict | None]
SessionResetConsumer = Callable[[], dict]

# Permission envelopes (JSON shape the host agent expects on stdout).
PERMISSION_REQUEST = "permission_request"  # Claude/Codex PermissionRequest decision
BEHAVIOR = "behavior"                      # Copilot {"behavior": ...}
PRETOOL_DECISION = "pretool_decision"      # VSCode PreToolUse permissionDecision
CURSOR_PERMISSION = "cursor_permission"    # Cursor {"permission": allow|deny|ask}

# Question envelopes.
UPDATED_INPUT = "updated_input"            # rewrite the question tool's input
CONTEXT = "context"                        # deny + additionalContext carrying answers


@dataclass(frozen=True)
class HookMode:
    """One --hook token an agent's installed hooks invoke.

    ``mode`` strings are persisted in users' hook files and must stay stable.
    They also share a namespace with the generic status states
    (working/waiting/ended), which unmatched tokens fall through to.
    """

    mode: str
    kind: str                  # "permission" | "question"
    envelope: str
    default_agent_id: str      # used only when --agent is missing/empty
    requires_tool_use_id: bool = False  # VSCode PreToolUse fires for non-tool events too
    # Decision emitted when no remote decision arrives (daemon down/timeout).
    # E.g. Cursor's "ask" explicitly falls back to its own local prompt;
    # empty means emit nothing (the agent's default behavior applies).
    fallback_decision: str = ""


@dataclass(frozen=True)
class ListenerContext:
    """Daemon capabilities a ``background_listener`` reports into.

    An agent that has no hooks but exposes a live event stream (e.g. OpenCode's
    HTTP server SSE bus) declares a ``background_listener``; the daemon runs it
    in its own thread and passes this context. The listener maps the agent's
    events onto codelight state and remote-control requests, transport-agnostic:
    - ``report_status(session_id, state, agent_id=, cwd=)`` — working/waiting/
      idle/ended, exactly like a status hook event.
    - ``submit_permission(msg, responder)`` / ``submit_question(msg, responder)``
      — route a prompt through the shared remote-control manager; ``responder``
      is called with the decision (``{"decision": ...}`` / ``{"answers": ...}``)
      so the listener can reply over its own transport (an HTTP POST for
      OpenCode). ``decision``/``answers`` of ``None`` means no remote answer —
      let the agent fall back to its own prompt.
    """

    shutdown: threading.Event
    report_status: Callable[..., None]
    submit_permission: Callable[[dict, Callable[[dict], None]], None]
    submit_question: Callable[[dict, Callable[[dict], None]], None]
    # Cancel this session's still-pending codelight prompts (e.g. the user
    # answered in the agent's own TUI, so the phone/GNOME card should clear).
    cancel_session_prompts: Callable[[str], None] = lambda _s: None
    log: Callable[[str], None] = lambda _m: None
    notify_conversation_changed: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display: str
    executables: tuple[str, ...] = ()
    vscode_extensions: frozenset[str] = frozenset()
    # Read-only tools of this agent that are safe to auto-allow when the
    # session cwd is inside a user-trusted folder.
    trusted_auto_allow_tools: frozenset[str] = frozenset()
    # Client-facing branding, shipped over the wire so clients need no
    # per-agent assets. logo_svg must fill with currentColor so clients can
    # tint it (status or brand color); keep it small — every client gets it
    # in the connect handshake.
    color: str = ""      # brand color, #rrggbb
    logo_svg: str = ""
    # Pre-rasterized 48x48 1-bit bitmap (MSB first, 288 bytes, base64) for
    # clients that cannot render SVG (the ESP8266 screen). Rasterize from
    # logo_svg when adding an agent.
    logo_bitmap: str = ""


@dataclass(frozen=True)
class AgentIntegration:
    """Everything the registry needs to know about one agent.

    Each agent module exports ``build_integration(config, ...)`` returning
    one of these, where ``config`` is the agent's section of the user's
    ~/.config/codelight/config.json (all keys optional; the module supplies
    defaults). Adding a built-in agent means adding a module that exports
    ``build_integration`` — no registry or client branching.

    ``install_hooks`` is keyword-only: (script_path, hook_wait_ceiling,
    remote_permissions, remote_questions, permission_timeout, log).
    """

    spec: AgentSpec
    # The module's live agent object (usage fetching, transcript lookups).
    agent: object = None
    hook_modes: tuple[HookMode, ...] = ()
    usage_fetcher: UsageFetcher | None = None
    session_reset_consumer: SessionResetConsumer | None = None
    install_hooks: Callable[..., None] | None = None
    removable_hook_paths: tuple[str, ...] = ()   # files to strip codelight hooks from
    removable_files: tuple[str, ...] = ()        # files codelight owns outright
    removable_empty_dirs: tuple[str, ...] = ()
    transcript_path_for_session: Callable[[str], str] | None = None
    latest_transcript_fallback: Callable[[], str] | None = None
    # Sniffs one transcript JSONL record; see transcript.TranscriptExtractor.
    transcript_extractor: Callable[..., "tuple[str, object] | None"] | None = None
    # Hookless agents that expose a live event stream declare a listener the
    # daemon runs in its own thread with a ListenerContext (e.g. OpenCode).
    background_listener: Callable[["ListenerContext"], None] | None = None
