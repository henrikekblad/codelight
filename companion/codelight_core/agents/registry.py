from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from codelight_core.agents import claude as claude_agent
from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent


UsageFetcher = Callable[[], dict | None]
Logger = Callable[[str], None]


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display: str
    executables: tuple[str, ...] = ()
    vscode_extensions: frozenset[str] = frozenset()


class AgentRegistry:
    """Central registry for supported agent integrations.

    The rest of the companion should ask this object for agent-owned behavior
    instead of importing individual agent modules. That keeps adding a future
    agent close to an additive change: add the agent module and register it
    here, then progressively move any special hook/protocol quirks behind the
    same interface.
    """

    def __init__(
        self,
        *,
        claude_settings_path: str,
        claude_credentials_path: str,
        codex_home: str,
        copilot_home: str,
        claude_usage_api: str = claude_agent.USAGE_API,
        github_org: str = "",
        github_token_file: str = "",
        github_api: Callable[[str, str], dict] | None = None,
        log: Logger | None = None,
    ) -> None:
        self.claude_settings_path = claude_settings_path
        self.claude_credentials_path = claude_credentials_path
        self.claude_usage_api = claude_usage_api
        self.codex_home = codex_home
        self.copilot_home = copilot_home
        self.github_org = github_org
        self.github_token_file = github_token_file
        self.log = log

        self.claude = claude_agent.ClaudeAgent(
            claude_credentials_path,
            usage_api=claude_usage_api,
            log=log,
        )
        self.codex = codex_agent.CodexAgent(codex_home)
        self.copilot = copilot_agent.CopilotAgent(
            github_org,
            copilot_home=copilot_home,
            token_file=github_token_file,
            api=github_api,
            log=log,
        )

        self.specs: dict[str, AgentSpec] = {
            "claude": AgentSpec(
                "claude",
                "Claude",
                executables=("claude",),
                vscode_extensions=frozenset({"anthropic.claude-code"}),
            ),
            "copilot": AgentSpec(
                "copilot",
                "Copilot",
                executables=("copilot",),
                vscode_extensions=frozenset({"github.copilot", "github.copilot-chat"}),
            ),
            "codex": AgentSpec(
                "codex",
                "Codex",
                executables=("codex",),
                vscode_extensions=frozenset({"openai.chatgpt"}),
            ),
        }

    def display_registry(self) -> dict[str, dict[str, str]]:
        return {
            agent_id: {"display": spec.display}
            for agent_id, spec in self.specs.items()
        }

    def supported_agent_ids(self) -> set[str]:
        return set(self.specs)

    def executables_by_agent(self) -> dict[str, tuple[str, ...]]:
        return {
            agent_id: spec.executables
            for agent_id, spec in self.specs.items()
            if spec.executables
        }

    def vscode_extensions_by_agent(self) -> dict[str, set[str]]:
        return {
            agent_id: set(spec.vscode_extensions)
            for agent_id, spec in self.specs.items()
            if spec.vscode_extensions
        }

    def usage_fetchers(self) -> dict[str, UsageFetcher]:
        return {
            "claude": self.claude.get_usage,
            "codex": self.codex.get_usage,
            "copilot": self.copilot.get_usage,
        }

    def github_token(self) -> str:
        return self.copilot.token()

    def install_hooks(
        self,
        *,
        enabled_agents: set[str],
        script_path: str,
        hook_wait_ceiling: int,
        remote_permissions: bool = False,
        remote_questions: bool = False,
        permission_timeout: int = 60,
        log=None,
    ) -> None:
        if "claude" in enabled_agents:
            claude_agent.install_hooks(
                self.claude_settings_path,
                script_path,
                hook_wait_ceiling=hook_wait_ceiling,
                remote_permissions=remote_permissions,
                remote_questions=remote_questions,
                permission_timeout=permission_timeout,
                vprint=log,
            )
        if "copilot" in enabled_agents:
            copilot_agent.install_hooks(
                copilot_agent.hooks_path(self.copilot_home),
                script_path,
                hook_wait_ceiling=hook_wait_ceiling,
                remote_permissions=remote_permissions,
                permission_timeout=permission_timeout,
            )
        if "codex" in enabled_agents:
            codex_agent.install_hooks(
                codex_agent.hooks_path(self.codex_home),
                script_path,
                hook_wait_ceiling=hook_wait_ceiling,
                remote_permissions=remote_permissions,
                remote_questions=remote_questions,
                permission_timeout=permission_timeout,
                vprint=log,
            )

    def removable_hook_paths(self) -> list[str]:
        return [
            self.claude_settings_path,
            codex_agent.hooks_path(self.codex_home),
        ]

    def removable_files(self) -> list[str]:
        return [copilot_agent.hooks_path(self.copilot_home)]

    def removable_empty_dirs(self) -> list[str]:
        return [os.path.dirname(copilot_agent.hooks_path(self.copilot_home))]

    def transcript_path_for_session(self, agent_id: str, session_id: str) -> str:
        if agent_id == "copilot":
            return self.copilot.events_path_for_session(session_id)
        if agent_id == "codex":
            return self.codex.rollout_path_for_session(session_id)
        return ""

    def latest_transcript_fallbacks(self) -> list[tuple[str, str]]:
        # Copilot hooks do not always pass a transcript path, so keep the old
        # behavior of falling back to its newest local events file.
        return [("copilot", self.copilot.latest_events_path())]
