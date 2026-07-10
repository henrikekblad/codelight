from __future__ import annotations

import os
import threading
import time
from typing import Callable


ActivePath = Callable[[], str]
HasClients = Callable[[], bool]
Broadcast = Callable[[], None]
ParseTranscript = Callable[[str], list[dict]]
ConversationAgent = Callable[[str], str]
AgentDisplay = Callable[[str | None], str]
FileSignature = tuple[int, int]


def build_payload(
    *,
    active_transcript: Callable[[], tuple[str, str]],
    parse_transcript: ParseTranscript,
    conversation_agent: ConversationAgent,
    agent_display_name: AgentDisplay,
) -> dict | None:
    """Build the conversation feed payload for the active transcript."""
    session_id, path = active_transcript()
    if not path:
        return None
    agent_id = conversation_agent(session_id)
    lines = parse_transcript(path)
    for line in lines:
        if isinstance(line, dict):
            line.setdefault("agent_id", agent_id)
            line.setdefault("agent_display", agent_display_name(agent_id))
    return {
        "type": "conversation",
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_display": agent_display_name(agent_id),
        "lines": lines,
    }


class ConversationRefresher:
    """Refresh conversation clients when the active transcript changes.

    Hooks call notify() whenever they know a transcript may have grown. The
    thread also does a slower fallback check so final assistant lines that are
    flushed just after Stop are still picked up.
    """

    def __init__(
        self,
        *,
        active_path: ActivePath,
        has_clients: HasClients,
        broadcast: Broadcast,
        shutdown: threading.Event,
        fallback_interval: float = 10.0,
        settle_window: float = 2.0,
        settle_interval: float = 0.25,
    ) -> None:
        self.active_path = active_path
        self.has_clients = has_clients
        self.broadcast = broadcast
        self.shutdown = shutdown
        self.fallback_interval = fallback_interval
        self.settle_window = settle_window
        self.settle_interval = settle_interval
        self._event = threading.Event()
        self._last_signature: FileSignature | None = None

    def notify(self) -> None:
        self._event.set()

    def refresh_if_changed(self) -> bool:
        if not self.has_clients():
            return False
        path = self.active_path()
        if not path:
            return False
        try:
            stat = os.stat(path)
        except OSError:
            return False
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._last_signature:
            return False
        self._last_signature = signature
        self.broadcast()
        return True

    def run(self) -> None:
        while not self.shutdown.is_set():
            signalled = self._event.wait(self.fallback_interval)
            self._event.clear()
            self.refresh_if_changed()

            # Hook notifications often arrive just before a transcript flush.
            # Keep a short eye on the same file without going back to a
            # permanent tight polling loop.
            if signalled and self.settle_window > 0:
                deadline = time.time() + self.settle_window
                while not self.shutdown.is_set() and time.time() < deadline:
                    self.shutdown.wait(self.settle_interval)
                    self.refresh_if_changed()
