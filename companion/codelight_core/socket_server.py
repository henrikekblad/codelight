from __future__ import annotations

import os
import socket
from collections.abc import Callable
from typing import Any

from codelight_core import hook_io


LogCallback = Callable[[str], None]
MessageHandler = Callable[[Any, dict], bool]


def serve_hook_socket(
    *,
    socket_path: str,
    shutdown,
    handle_message: MessageHandler,
    log: LogCallback,
) -> None:
    """Accept hook events on the Unix socket and dispatch parsed JSON messages.

    `handle_message` returns True when it takes ownership of the connection
    (permission/question hooks block on that connection until resolved).
    """
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(32)
    server.settimeout(1.0)
    log(f"[socket] listening on {socket_path}")

    try:
        while not shutdown.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue

            taken = False
            try:
                conn.settimeout(2.0)
                message = hook_io.read_json_message(conn, max_bytes=8192)
                if message is None:
                    continue
                taken = handle_message(conn, message)
            except Exception as e:
                log(f"[socket] error: {e}")
            finally:
                if not taken:
                    conn.close()
    finally:
        server.close()
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
