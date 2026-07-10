from __future__ import annotations

from typing import Callable, Sequence

from codelight_core.agents import claude as claude_agent
from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent
from codelight_core.agents.base import (
    AgentIntegration,
    AgentSpec,
    HookMode,
    UsageFetcher,
)


Logger = Callable[[str], None]


class AgentRegistry:
    """Central registry for supported agent integrations.

    The rest of the companion should ask this object for agent-owned behavior
    instead of importing individual agent modules. Each agent module exports
    ``build_integration(...)``; adding an agent means adding its module to the
    integration list below (or injecting one via ``extra_agents``) — clients
    need no changes.
    """

    def __init__(
        self,
        *,
        agents_config: dict | None = None,
        claude_usage_api: str = claude_agent.USAGE_API,
        github_api: Callable[[str, str], dict] | None = None,
        log: Logger | None = None,
        extra_agents: Sequence[AgentIntegration] = (),
    ) -> None:
        """``agents_config`` is the "agents" section of the user's
        ~/.config/codelight/config.json: {agent_id: {key: value}}. Each agent
        module documents and consumes its own keys; everything is optional.
        """
        config = agents_config or {}

        def section(agent_id: str) -> dict:
            value = config.get(agent_id)
            return value if isinstance(value, dict) else {}

        integrations = [
            claude_agent.build_integration(
                section("claude"), usage_api=claude_usage_api, log=log),
            copilot_agent.build_integration(
                section("copilot"), api=github_api, log=log),
            codex_agent.build_integration(section("codex")),
            *extra_agents,
        ]
        self._integrations: dict[str, AgentIntegration] = {}
        for integration in integrations:
            agent_id = integration.spec.agent_id
            if agent_id in self._integrations:
                raise ValueError(f"duplicate agent id: {agent_id}")
            self._integrations[agent_id] = integration
        self.specs: dict[str, AgentSpec] = {
            agent_id: integration.spec
            for agent_id, integration in self._integrations.items()
        }

    @property
    def default_agent_id(self) -> str:
        """Fallback agent for events that don't carry an agent id.

        The first registered integration; also the agent whose usage meters
        are shown when nothing is active.
        """
        return next(iter(self._integrations))

    def agent(self, agent_id: str) -> object | None:
        """The agent module's live agent object, or None if unknown."""
        integration = self._integrations.get(agent_id)
        return integration.agent if integration else None

    def display_registry(self) -> dict[str, dict[str, str]]:
        return {
            agent_id: {"display": spec.display}
            for agent_id, spec in self.specs.items()
        }

    # The screen has ~45 KB of RAM; bound the agents map it must parse.
    MAX_SCREEN_AGENTS = 6

    def client_metadata(self, client: str = "") -> dict[str, dict[str, str]]:
        """Per-agent branding shipped to a client in the config handshake.

        The ESP8266 screen cannot render SVG, so it gets the pre-rasterized
        1-bit bitmaps instead, capped to MAX_SCREEN_AGENTS entries.
        """
        if client == "screen":
            return {
                agent_id: {
                    "display": spec.display,
                    "color": spec.color,
                    "logo_bitmap": spec.logo_bitmap,
                }
                for agent_id, spec in
                list(self.specs.items())[:self.MAX_SCREEN_AGENTS]
            }
        return {
            agent_id: {
                "display": spec.display,
                "color": spec.color,
                "logo_svg": spec.logo_svg,
            }
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

    def trusted_auto_allow_tools(self, agent_id: str) -> frozenset[str]:
        spec = self.specs.get(agent_id)
        return spec.trusted_auto_allow_tools if spec else frozenset()

    def hook_modes(self) -> dict[str, HookMode]:
        modes: dict[str, HookMode] = {}
        for integration in self._integrations.values():
            for mode in integration.hook_modes:
                if mode.mode in modes:
                    raise ValueError(f"duplicate hook mode: {mode.mode}")
                modes[mode.mode] = mode
        return modes

    def usage_fetchers(self) -> dict[str, UsageFetcher]:
        return {
            agent_id: integration.usage_fetcher
            for agent_id, integration in self._integrations.items()
            if integration.usage_fetcher is not None
        }

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
        for agent_id, integration in self._integrations.items():
            if agent_id not in enabled_agents or integration.install_hooks is None:
                continue
            integration.install_hooks(
                script_path=script_path,
                hook_wait_ceiling=hook_wait_ceiling,
                remote_permissions=remote_permissions,
                remote_questions=remote_questions,
                permission_timeout=permission_timeout,
                log=log,
            )

    def removable_hook_paths(self) -> list[str]:
        return [
            path
            for integration in self._integrations.values()
            for path in integration.removable_hook_paths
        ]

    def removable_files(self) -> list[str]:
        return [
            path
            for integration in self._integrations.values()
            for path in integration.removable_files
        ]

    def removable_empty_dirs(self) -> list[str]:
        return [
            path
            for integration in self._integrations.values()
            for path in integration.removable_empty_dirs
        ]

    def transcript_path_for_session(self, agent_id: str, session_id: str) -> str:
        integration = self._integrations.get(agent_id)
        if integration is None or integration.transcript_path_for_session is None:
            return ""
        return integration.transcript_path_for_session(session_id)

    def transcript_extractors(self) -> tuple[Callable, ...]:
        return tuple(
            integration.transcript_extractor
            for integration in self._integrations.values()
            if integration.transcript_extractor is not None
        )

    def latest_transcript_fallbacks(self) -> list[tuple[str, str]]:
        return [
            (agent_id, integration.latest_transcript_fallback())
            for agent_id, integration in self._integrations.items()
            if integration.latest_transcript_fallback is not None
        ]
