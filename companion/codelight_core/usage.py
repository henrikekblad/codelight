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
    usages: dict[str, dict | None],
    display_name: Callable[[str], str] | None = None,
) -> str:
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


class UsagePoller:
    """Poll supported agent usage and publish updated state snapshots."""

    def __init__(
        self,
        *,
        state: CodelightState,
        fetchers: dict[str, UsageFetcher],
        interval: int,
        shutdown: threading.Event,
        log: Logger,
        push: Push,
    ) -> None:
        self.state = state
        self.fetchers = dict(fetchers)
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
            if result is not None or agent_id != self.state.default_agent_id:
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
