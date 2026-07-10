from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

try:
    from dbus_fast import BusType as _DbusBusType
    from dbus_fast.aio import MessageBus as _DbusMessageBus
    from dbus_fast.service import ServiceInterface as _DbusServiceInterface
    from dbus_fast.service import method as _dbus_method
    from dbus_fast.service import signal as _dbus_signal

    HAVE_DBUS = True
except ImportError:
    HAVE_DBUS = False


StatusCallback = Callable[[], dict[str, Any]]
ConfigCallback = Callable[[str], dict[str, Any]]
PermissionCallback = Callable[[str, str], bool]
QuestionCallback = Callable[[str, Any], bool]
ExtendCallback = Callable[[str], bool]
AnnounceCallback = Callable[[list[str]], bool]
LogCallback = Callable[[str], None]


def available() -> bool:
    return HAVE_DBUS


if HAVE_DBUS:

    class CodelightDbusInterface(_DbusServiceInterface):  # type: ignore[misc]
        def __init__(
            self,
            *,
            status_snapshot: StatusCallback,
            client_config: ConfigCallback,
            respond_permission: PermissionCallback,
            respond_question: QuestionCallback,
            extend_request: ExtendCallback,
            announce: AnnounceCallback,
        ):
            super().__init__("se.sensnology.codelight")
            self._status_snapshot = status_snapshot
            self._client_config = client_config
            self._respond_permission = respond_permission
            self._respond_question = respond_question
            self._extend_request = extend_request
            self._announce = announce

        @_dbus_signal()
        def StatusChanged(self, status_json: str) -> "s":  # type: ignore[return]
            return status_json

        @_dbus_method()
        def GetStatus(self) -> "s":  # type: ignore[return]
            return json.dumps(self._status_snapshot())

        @_dbus_method()
        def GetConfig(self, client: "s") -> "s":  # type: ignore[return]
            # One-time client config (agent branding etc.), mirroring the
            # "config" message WebSocket clients get on subscribe. ``client``
            # is the caller's type (e.g. "gnome") and selects the variant.
            return json.dumps({"type": "config", **self._client_config(client)})

        @_dbus_signal()
        def PermissionRequest(self, request_json: str) -> "s":  # type: ignore[return]
            return request_json

        @_dbus_signal()
        def PermissionResolved(self, resolved_json: str) -> "s":  # type: ignore[return]
            return resolved_json

        @_dbus_method()
        def RespondPermission(self, request_id: "s", decision: "s") -> "b":  # type: ignore[return]
            # Session bus = same local user → inside the trust boundary.
            return self._respond_permission(request_id, decision)

        @_dbus_signal()
        def QuestionRequest(self, request_json: str) -> "s":  # type: ignore[return]
            return request_json

        @_dbus_signal()
        def QuestionResolved(self, resolved_json: str) -> "s":  # type: ignore[return]
            return resolved_json

        @_dbus_method()
        def RespondQuestion(self, request_id: "s", answers_json: "s") -> "b":  # type: ignore[return]
            try:
                answers = json.loads(answers_json)
            except Exception:
                return False
            return self._respond_question(request_id, answers)

        @_dbus_method()
        def ExtendRequest(self, request_id: "s") -> "b":  # type: ignore[return]
            # Keepalive while the GNOME prompt is open, so it doesn't time out.
            return self._extend_request(request_id)

        @_dbus_method()
        def Announce(self, features_json: "s") -> "b":  # type: ignore[return]
            # The GNOME extension announces (on enable + a periodic heartbeat)
            # which features it can answer, so question fall-through doesn't fire
            # while it's listening. Not a WS subscriber, so it can't be counted
            # any other way.
            try:
                features = json.loads(features_json)
            except Exception:
                features = []
            if not isinstance(features, list):
                features = []
            return self._announce([str(feature) for feature in features])


async def export(
    *,
    status_snapshot: StatusCallback,
    client_config: ConfigCallback,
    respond_permission: PermissionCallback,
    respond_question: QuestionCallback,
    extend_request: ExtendCallback,
    announce: AnnounceCallback,
    log: LogCallback,
) -> object | None:
    if not HAVE_DBUS:
        return None
    try:
        dbus_bus = await _DbusMessageBus(bus_type=_DbusBusType.SESSION).connect()
        iface = CodelightDbusInterface(
            status_snapshot=status_snapshot,
            client_config=client_config,
            respond_permission=respond_permission,
            respond_question=respond_question,
            extend_request=extend_request,
            announce=announce,
        )
        dbus_bus.export("/se/sensnology/codelight", iface)
        await dbus_bus.request_name("se.sensnology.codelight")
        iface._codelight_bus = dbus_bus  # keep the bus alive with the interface
        log("[dbus] service exported")
        return iface
    except Exception as e:
        print(f"[dbus] setup failed: {e}", file=sys.stderr, flush=True)
        return None
