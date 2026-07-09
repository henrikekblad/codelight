from __future__ import annotations

import sys
import threading
from typing import Callable

from codelight_core.state import CodelightState


UsageFetcher = Callable[[], dict | None]
Logger = Callable[[str], None]
Push = Callable[[], None]


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
