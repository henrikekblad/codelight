from __future__ import annotations

from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent


class AgentRuntime:
    """Runtime lookups for agent-owned local files.

    This keeps the daemon loop from knowing each agent's on-disk session layout.
    Adding a future agent should mean teaching this adapter how to construct the
    new agent module, not adding more branches in codelight.py.
    """

    def __init__(self, *, codex_home: str, copilot_home: str) -> None:
        self.codex = codex_agent.CodexAgent(codex_home)
        self.copilot = copilot_agent.CopilotAgent(copilot_home=copilot_home)

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
