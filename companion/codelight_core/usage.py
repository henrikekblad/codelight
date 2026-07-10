from __future__ import annotations

import sys
import threading
from typing import Callable

from codelight_core.agents import claude as claude_agent
from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent
from codelight_core.state import CodelightState


UsageFetcher = Callable[[], dict | None]
Logger = Callable[[str], None]
Push = Callable[[], None]


class UsageFetchers:
    def __init__(
        self,
        *,
        claude_credentials_path: str,
        claude_usage_api: str,
        codex_home: str,
        copilot_home: str,
        github_org: str = "",
        github_token_file: str = "",
        github_api: Callable[[str, str], dict] | None = None,
        log: Logger | None = None,
    ) -> None:
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

    def get_claude_usage(self) -> dict | None:
        return self.claude.get_usage()

    def codex_usage_from_rollout(self, path: str) -> dict | None:
        return self.codex.usage_from_rollout(path)

    def get_codex_usage(self) -> dict | None:
        return self.codex.get_usage()

    def github_token(self) -> str:
        return self.copilot.token()

    def get_copilot_usage(self, *, org: str | None = None,
                          token: str | None = None,
                          now=None) -> dict | None:
        return self.copilot.get_usage(org=org, token=token, now=now)

    def usage_fetchers(self) -> dict[str, UsageFetcher]:
        return {
            "claude": self.get_claude_usage,
            "codex": self.get_codex_usage,
            "copilot": self.get_copilot_usage,
        }


def usage_summary(
    *,
    usages: dict[str, dict | None] | None = None,
    display_name: Callable[[str], str] | None = None,
    claude: dict | None = None,
    codex: dict | None = None,
    copilot: dict | None = None,
) -> str:
    if usages is not None:
        parts: list[str] = []
        for agent_id, usage in usages.items():
            if usage is None:
                continue
            display = display_name(agent_id) if display_name else agent_id.capitalize()
            if "monthly_pct" in usage and "weekly_pct" not in usage:
                parts.append(f"{display} {usage['monthly_pct']:.0%}")
            elif "session_pct" in usage and "weekly_pct" in usage:
                parts.append(
                    f"{display} {usage['session_pct']:.0%}/{usage['weekly_pct']:.0%}")
        return "  ".join(parts)

    parts: list[str] = []
    if claude is not None:
        parts.append(f"Claude {claude['session_pct']:.0%}/{claude['weekly_pct']:.0%}")
    if codex is not None:
        parts.append(f"Codex {codex['session_pct']:.0%}/{codex['weekly_pct']:.0%}")
    if copilot is not None:
        parts.append(f"Copilot {copilot['monthly_pct']:.0%}")
    return "  ".join(parts)


class UsagePoller:
    """Poll supported agent usage and publish updated state snapshots."""

    def __init__(
        self,
        *,
        state: CodelightState,
        fetchers: dict[str, UsageFetcher] | None = None,
        fetch_claude: UsageFetcher | None = None,
        fetch_codex: UsageFetcher | None = None,
        fetch_copilot: UsageFetcher | None = None,
        interval: int,
        shutdown: threading.Event,
        log: Logger,
        push: Push,
    ) -> None:
        self.state = state
        self.fetchers = dict(fetchers or {})
        if fetch_claude is not None:
            self.fetchers["claude"] = fetch_claude
        if fetch_codex is not None:
            self.fetchers["codex"] = fetch_codex
        if fetch_copilot is not None:
            self.fetchers["copilot"] = fetch_copilot
        self.interval = interval
        self.shutdown = shutdown
        self.log = log
        self.push = push

    def poll_once(self) -> None:
        self.log("[usage] polling…")
        results: dict[str, dict | None] = {}
        updates: dict[str, dict | None] = {}
        for agent_id, fetch in self.fetchers.items():
            try:
                result = fetch()
            except Exception as e:
                print(f"[usage] {agent_id} error: {e}", file=sys.stderr, flush=True)
                result = None
            results[agent_id] = result
            if result is not None or agent_id != "claude":
                updates[agent_id] = result

        self.state.update_usage(usages=updates)

        summary = usage_summary(
            usages=results,
            display_name=self.state.agent_display_name,
        )
        if summary:
            self.log("[usage] " + summary)
        else:
            self.log("[usage] no data from any agent")

        self.push()

    def run(self) -> None:
        while not self.shutdown.is_set():
            self.poll_once()
            self.shutdown.wait(self.interval)
