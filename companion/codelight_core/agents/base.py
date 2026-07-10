"""Shared agent-integration types.

Leaf module: agent modules, the registry, and the generic hook runtime all
import it, so it must not import other codelight_core modules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


UsageFetcher = Callable[[], dict | None]

# Permission envelopes (JSON shape the host agent expects on stdout).
PERMISSION_REQUEST = "permission_request"  # Claude/Codex PermissionRequest decision
BEHAVIOR = "behavior"                      # Copilot {"behavior": ...}
PRETOOL_DECISION = "pretool_decision"      # VSCode PreToolUse permissionDecision

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


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display: str
    executables: tuple[str, ...] = ()
    vscode_extensions: frozenset[str] = frozenset()
    # Read-only tools of this agent that are safe to auto-allow when the
    # session cwd is inside a user-trusted folder.
    trusted_auto_allow_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AgentIntegration:
    """Everything the registry needs to know about one agent.

    Each agent module exports ``build_integration(...)`` returning one of
    these; adding an agent means adding a module and registering its
    integration — no registry or client branching.

    ``install_hooks`` is keyword-only: (script_path, hook_wait_ceiling,
    remote_permissions, remote_questions, permission_timeout, log).
    """

    spec: AgentSpec
    hook_modes: tuple[HookMode, ...] = ()
    usage_fetcher: UsageFetcher | None = None
    install_hooks: Callable[..., None] | None = None
    removable_hook_paths: tuple[str, ...] = ()   # files to strip codelight hooks from
    removable_files: tuple[str, ...] = ()        # files codelight owns outright
    removable_empty_dirs: tuple[str, ...] = ()
    transcript_path_for_session: Callable[[str], str] | None = None
    latest_transcript_fallback: Callable[[], str] | None = None
    # Sniffs one transcript JSONL record; see transcript.TranscriptExtractor.
    transcript_extractor: Callable[..., "tuple[str, object] | None"] | None = None
