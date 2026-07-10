from __future__ import annotations

import json
import os
import socket
import time


def send_json(socket_path: str, payload: dict, *, timeout: float,
              newline: bool = False) -> bool:
    """Best-effort fire-and-forget JSON send to the daemon Unix socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
        raw = json.dumps(payload)
        if newline:
            raw += "\n"
        sock.sendall(raw.encode())
        sock.close()
        return True
    except Exception:
        return False


def request_json(socket_path: str, payload: dict, *, connect_timeout: float,
                 response_timeout: float, max_bytes: int) -> dict | None:
    """Send a JSON request and read one newline-delimited JSON response."""
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(connect_timeout)
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload) + "\n").encode())

        sock.settimeout(response_timeout)
        buf = b""
        while b"\n" not in buf and len(buf) < max_bytes:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            return None
        data = json.loads(buf.decode())
        return data if isinstance(data, dict) else None
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def read_json_message(conn, *, max_bytes: int) -> dict | None:
    """Read one JSON object from a socket, stopping at newline/EOF/max_bytes."""
    try:
        raw = b""
        while b"\n" not in raw and len(raw) < max_bytes:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk
        if not raw.strip():
            return None
        line = raw.split(b"\n", 1)[0]
        data = json.loads(line.decode())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_monitor_state(
    state_dir: str,
    *,
    session_id: str,
    state: str,
    agent_id: str,
    hook_event: str = "",
) -> None:
    """Fallback state file used when the daemon socket is unavailable."""
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, f"{session_id}.json")
    if state == "ended":
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    try:
        with open(path, "w") as f:
            json.dump({
                "state": state,
                "time": time.time(),
                "session_id": session_id,
                "agent_id": agent_id,
                "hook_event": hook_event,
            }, f)
    except Exception:
        pass
