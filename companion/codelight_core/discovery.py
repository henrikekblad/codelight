from __future__ import annotations

import socket
import sys
import threading
from typing import Callable

try:
    from zeroconf import ServiceInfo, Zeroconf

    HAVE_ZEROCONF = True
except ImportError:
    HAVE_ZEROCONF = False


Logger = Callable[[str], None]


def get_local_ip() -> str:
    """Return the LAN IP this machine uses for outbound traffic."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _close_registration(zeroconf, info) -> None:
    if info is not None and zeroconf is not None:
        try:
            zeroconf.unregister_service(info)
        except Exception:
            pass
    if zeroconf is not None:
        try:
            zeroconf.close()
        except Exception:
            pass


def advertise_mdns(
    *,
    port: int,
    name: str,
    shutdown: threading.Event,
    log: Logger,
    verbose_log: Logger | None = None,
    local_ip: Callable[[], str] = get_local_ip,
    zeroconf_cls=None,
    service_info_cls=None,
) -> None:
    """Advertise the WebSocket service via mDNS and re-register on IP changes."""
    if (zeroconf_cls is None or service_info_cls is None) and not HAVE_ZEROCONF:
        unavailable_message()
        return
    zeroconf_cls = zeroconf_cls or Zeroconf
    service_info_cls = service_info_cls or ServiceInfo

    zc = None
    current_ip: str | None = None
    info = None
    while not shutdown.is_set():
        ip = local_ip()

        if ip.startswith("127."):
            if current_ip is not None:
                _close_registration(zc, info)
                zc = None
                info = None
                current_ip = None
                log("[mdns] network lost, waiting for reconnect…")
            shutdown.wait(5)
            continue

        if ip != current_ip:
            _close_registration(zc, info)
            zc = None
            info = None
            try:
                zc = zeroconf_cls(interfaces=[ip])
                info = service_info_cls(
                    "_codelight._tcp.local.",
                    f"{name}._codelight._tcp.local.",
                    addresses=[socket.inet_aton(ip)],
                    port=port,
                    properties={},
                )
                zc.register_service(info)
                current_ip = ip
                log(f"[mdns] advertising on {ip}:{port}")
            except Exception as e:
                log(f"[mdns] registration failed: {e}")
                _close_registration(zc, None)
                zc = None
                info = None
                shutdown.wait(5)
                continue

        shutdown.wait(10)

    _close_registration(zc, info)
    if verbose_log:
        verbose_log("[mdns] stopped")


def unavailable_message() -> None:
    print("[mdns] zeroconf not installed — skipping advertisement", file=sys.stderr)
    print("[mdns] Install: pip install zeroconf", file=sys.stderr)
