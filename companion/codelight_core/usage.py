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


def usage_summary(
    *,
    claude: dict | None = None,
    codex: dict | None = None,
    copilot: dict | None = None,
) -> str:
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
        fetch_claude: UsageFetcher,
        fetch_codex: UsageFetcher,
        fetch_copilot: UsageFetcher,
        interval: int,
        shutdown: threading.Event,
        log: Logger,
        push: Push,
    ) -> None:
        self.state = state
        self.fetch_claude = fetch_claude
        self.fetch_codex = fetch_codex
        self.fetch_copilot = fetch_copilot
        self.interval = interval
        self.shutdown = shutdown
        self.log = log
        self.push = push

    def poll_once(self) -> None:
        self.log("[usage] polling…")
        try:
            claude = self.fetch_claude()
            codex = self.fetch_codex()
            copilot = self.fetch_copilot()
        except Exception as e:
            print(f"[usage] unexpected error: {e}", file=sys.stderr, flush=True)
            claude = None
            codex = None
            copilot = None

        self.state.update_usage(
            claude=claude,
            codex=codex,
            copilot=copilot,
            clear_codex=codex is None,
            clear_copilot=copilot is None,
        )

        summary = usage_summary(claude=claude, codex=codex, copilot=copilot)
        if summary:
            self.log("[usage] " + summary)
        else:
            self.log("[usage] no data from any agent")

        self.push()

    def run(self) -> None:
        while not self.shutdown.is_set():
            self.poll_once()
            self.shutdown.wait(self.interval)
