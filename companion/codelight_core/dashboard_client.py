from __future__ import annotations

import asyncio
import json
import sys
from typing import Callable

from codelight_core import auth
from codelight_core import presentation


def _clients_from_payload(payload: dict) -> tuple[int, bool]:
    clients = payload.get("clients")
    if not isinstance(clients, dict):
        return 0, False
    try:
        ws_count = int(clients.get("websocket") or 0)
    except (TypeError, ValueError):
        ws_count = 0
    return ws_count, bool(clients.get("dbus"))


def render_payload(
    payload: dict,
    *,
    agent_registry: dict[str, dict[str, str]],
    default_agent_id: str,
    dashboard_ready: bool,
) -> str:
    ws_count, dbus_present = _clients_from_payload(payload)
    activity = payload.get("activity")
    if not isinstance(activity, list):
        activity = []
    return presentation.render_dashboard(
        payload,
        agent_registry=agent_registry,
        default_agent_id=default_agent_id,
        log_lines=[str(line) for line in activity],
        ws_count=ws_count,
        dbus_present=dbus_present,
        dashboard_ready=dashboard_ready,
    )


async def run(
    *,
    uri: str,
    secret: str,
    agent_registry: dict[str, dict[str, str]],
    default_agent_id: str,
    websockets_module,
    output=None,
    reconnect_delay: float = 2.0,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Render a terminal dashboard by consuming the daemon WebSocket payload."""
    out = output or sys.stdout
    dashboard_ready = False
    should_stop = stop or (lambda: False)

    while not should_stop():
        try:
            async with websockets_module.connect(uri) as ws:
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue

                    if payload.get("type") == "challenge":
                        nonce = str(payload.get("nonce") or "")
                        await ws.send(json.dumps({
                            "auth_hmac": auth.auth_hmac(secret, nonce),
                        }))
                        continue

                    if payload.get("type") == "config":
                        await ws.send(json.dumps({
                            "type": "subscribe",
                            "client": "cli-dashboard",
                            "features": [],
                        }))
                        continue

                    if "status" not in payload:
                        continue

                    out.write(render_payload(
                        payload,
                        agent_registry=agent_registry,
                        default_agent_id=default_agent_id,
                        dashboard_ready=dashboard_ready,
                    ))
                    out.flush()
                    dashboard_ready = True
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            prefix = "\033[2J\033[H" if not dashboard_ready else "\033[H"
            dashboard_ready = True
            out.write(
                prefix
                + "CODELIGHT\n"
                + f"  Dashboard disconnected: {exc}\033[K\n"
                + f"  Retrying {reconnect_delay:.0f}s…\033[K\033[J"
            )
            out.flush()
            await asyncio.sleep(reconnect_delay)
