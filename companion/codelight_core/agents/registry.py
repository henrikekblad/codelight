from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import Callable, Sequence

from codelight_core import agents as agents_pkg
from codelight_core.agents.base import (
    AgentIntegration,
    AgentSpec,
    HookMode,
    UsageFetcher,
)


Logger = Callable[[str], None]
BUILTIN_EXCLUDE = frozenset({"base", "registry"})


def discover_agent_modules() -> list[ModuleType]:
    """Import every built-in agent module that exposes build_integration().

    Adding a built-in integration should be additive: drop a new .py file into
    codelight_core/agents/ and export build_integration(config, ...). The
    registry owns ordering and duplicate checks, while the module owns all
    agent-specific behavior.
    """
    modules: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(agents_pkg.__path__),
                       key=lambda item: item.name):
        if info.name in BUILTIN_EXCLUDE or info.ispkg:
            continue
        module = importlib.import_module(f"{agents_pkg.__name__}.{info.name}")
        if callable(getattr(module, "build_integration", None)):
            modules.append(module)
    return modules


def _supported_kwargs(func: Callable, candidates: dict) -> dict:
    """Return only the keyword-only test/runtime hooks a builder accepts."""
    params = inspect.signature(func).parameters
    return {
        name: value
        for name, value in candidates.items()
        if name in params and value is not None
    }


class AgentRegistry:
    """Central registry for supported agent integrations.

    The rest of the companion should ask this object for agent-owned behavior
    instead of importing individual agent modules. Each agent module exports
    ``build_integration(...)``; adding an agent means adding its module to
    ``codelight_core/agents`` (or injecting one via ``extra_agents`` in tests)
    — clients need no changes.
    """

    def __init__(
        self,
        *,
        agents_config: dict | None = None,
        claude_usage_api: str | None = None,
        github_api: Callable[[str, str], dict] | None = None,
        log: Logger | None = None,
        modules: Sequence[ModuleType] | None = None,
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

        build_kwargs = {
            # Historical test hooks. The registry only passes them to modules
            # that declare the corresponding parameter.
            "usage_api": claude_usage_api,
            "api": github_api,
            "log": log,
        }
        integrations = []
        for module in modules if modules is not None else discover_agent_modules():
            builder = getattr(module, "build_integration")
            module_spec = getattr(module, "SPEC", None)
            provisional_id = getattr(
                module_spec,
                "agent_id",
                module.__name__.rsplit(".", 1)[-1],
            )
            integrations.append(builder(
                section(provisional_id),
                **_supported_kwargs(builder, build_kwargs),
            ))
        integrations.extend(extra_agents)
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
        conversation = self.conversation_agents()
        budget = self.budget_agents()
        return {
            agent_id: {
                "display": spec.display,
                "color": spec.color,
                "logo_svg": spec.logo_svg,
                "conversation": agent_id in conversation,
                "budget_settable": agent_id in budget,
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

    def background_listeners(self, agent_ids=None) -> dict[str, Callable]:
        """Agents (optionally filtered to ``agent_ids``) that expose a
        long-lived event-stream listener the daemon should run in a thread."""
        return {
            agent_id: integration.background_listener
            for agent_id, integration in self._integrations.items()
            if integration.background_listener is not None
            and (agent_ids is None or agent_id in agent_ids)
        }

    def budget_agents(self) -> set[str]:
        """Agents whose usage-meter budget the user can set from a client."""
        return {
            agent_id for agent_id, integration in self._integrations.items()
            if integration.budget_setter is not None
        }

    def get_budget(self, agent_id: str) -> float:
        integration = self._integrations.get(agent_id)
        if integration is None or integration.budget_getter is None:
            return 0.0
        return float(integration.budget_getter())

    def set_budget(self, agent_id: str, monthly_budget_usd: float) -> bool:
        integration = self._integrations.get(agent_id)
        if integration is None or integration.budget_setter is None:
            return False
        integration.budget_setter(monthly_budget_usd)
        return True

    def session_reset_supported(self, agent_id: str) -> bool:
        integration = self._integrations.get(agent_id)
        return bool(integration and integration.session_reset_consumer)

    def consume_session_reset(self, agent_id: str) -> dict:
        integration = self._integrations.get(agent_id)
        if integration is None or integration.session_reset_consumer is None:
            return {
                "ok": False,
                "outcome": "unsupported",
                "message": "Agent does not support session limit resets.",
            }
        return integration.session_reset_consumer()

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

    def conversation_agents(self) -> set[str]:
        """Agents that can produce a conversation feed — via a transcript
        extractor (file-based) or a conversation provider (API/DB-based)."""
        return {
            agent_id
            for agent_id, integration in self._integrations.items()
            if integration.transcript_extractor is not None
            or integration.conversation_provider is not None
        }

    def conversation_provider_for(self, agent_id: str):
        """The agent's non-file conversation provider, or None."""
        integration = self._integrations.get(agent_id)
        return integration.conversation_provider if integration else None

    def latest_transcript_for(self, agent_id: str) -> str:
        """Newest on-disk transcript for an agent, for cold-start requests
        (before any hook has been seen this run). Empty if unavailable."""
        integration = self._integrations.get(agent_id)
        if integration is None or integration.latest_transcript_fallback is None:
            return ""
        return integration.latest_transcript_fallback()
