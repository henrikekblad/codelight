from __future__ import annotations

import asyncio
import json
import secrets
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from codelight_core import auth as auth_core
from codelight_core import dbus_service


JsonDict = dict[str, Any]
LogCallback = Callable[[str], None]
StatusSnapshotCallback = Callable[[], JsonDict]
OverallStatusCallback = Callable[[], tuple[int, str, dict[str, str], str]]
PendingPayloadsCallback = Callable[[], list[JsonDict]]
ConversationPayloadCallback = Callable[[], JsonDict | None]
SimpleCallback = Callable[[], None]
PermissionResponseCallback = Callable[[str, str, str], bool]
QuestionResponseCallback = Callable[[str, Any, str], bool]
ExtendCallback = Callable[[str], bool]
AnnounceCallback = Callable[[list[str]], bool]


class CodelightWebsocketHub:
    """Owns WebSocket clients, subscription sets, and the D-Bus bridge."""

    def __init__(
        self,
        *,
        websockets_module,
        shutdown,
        remote_permissions: Callable[[], bool],
        remote_questions: Callable[[], bool],
        client_config: Callable[[str], JsonDict],
        status_snapshot: StatusSnapshotCallback,
        overall_status: OverallStatusCallback,
        pending_payloads: PendingPayloadsCallback,
        conversation_payload: ConversationPayloadCallback,
        notify_conversation_changed: SimpleCallback,
        note_question_client_gone: SimpleCallback,
        respond_permission: PermissionResponseCallback,
        respond_question: QuestionResponseCallback,
        extend_request: ExtendCallback,
        announce_gnome: AnnounceCallback,
        log: LogCallback,
        verbose_log: LogCallback,
    ):
        self._websockets = websockets_module
        self._shutdown = shutdown
        self._remote_permissions = remote_permissions
        self._remote_questions = remote_questions
        self._client_config = client_config
        self._status_snapshot = status_snapshot
        self._overall_status = overall_status
        self._pending_payloads = pending_payloads
        self._conversation_payload = conversation_payload
        self._notify_conversation_changed = notify_conversation_changed
        self._note_question_client_gone = note_question_client_gone
        self._respond_permission = respond_permission
        self._respond_question = respond_question
        self._extend_request = extend_request
        self._announce_gnome = announce_gnome
        self._log = log
        self._verbose_log = verbose_log

        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set = set()
        self.permission_clients: set = set()
        self.conversation_clients: set = set()
        self.question_clients: set = set()
        self.dbus_iface: object | None = None
        self.last_status = "idle"

    def client_count(self) -> int:
        return len(self.clients)

    def dbus_exported(self) -> bool:
        return self.dbus_iface is not None

    def has_conversation_clients(self) -> bool:
        return bool(self.conversation_clients)

    def has_question_clients(self) -> bool:
        return bool(self.question_clients)

    def broadcast_status(self, payload: JsonDict) -> None:
        self.last_status = str(payload.get("status", self.last_status))
        if self.loop is None:
            return
        msg = json.dumps(payload)

        async def _send_all() -> None:
            if self.clients:
                await asyncio.gather(
                    *[client.send(msg) for client in list(self.clients)],
                    return_exceptions=True,
                )
            self._emit_dbus("StatusChanged", msg)

        asyncio.run_coroutine_threadsafe(_send_all(), self.loop)

    def broadcast_remote(self, payload: JsonDict, dbus_signal: str) -> None:
        if self.loop is None:
            return
        msg = json.dumps(payload)

        async def _send() -> None:
            if self.permission_clients:
                await asyncio.gather(
                    *[client.send(msg) for client in list(self.permission_clients)],
                    return_exceptions=True,
                )
            self._emit_dbus(dbus_signal, msg)

        asyncio.run_coroutine_threadsafe(_send(), self.loop)

    def broadcast_conversation(self) -> None:
        if self.loop is None or not self.conversation_clients:
            return
        payload = self._conversation_payload()
        if payload is None:
            return
        msg = json.dumps(payload)

        async def _send() -> None:
            targets = [
                client for client in list(self.conversation_clients)
                if client in self.clients
            ]
            if targets:
                await asyncio.gather(
                    *[client.send(msg) for client in targets],
                    return_exceptions=True,
                )

        asyncio.run_coroutine_threadsafe(_send(), self.loop)

    def run(self, *, port: int, secret: str) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve(port=port, secret=secret))
        except Exception as e:
            print(f"[ws] server error: {e}", file=sys.stderr)
        finally:
            self.loop.close()
            self.loop = None

    async def _serve(self, *, port: int, secret: str) -> None:
        self.dbus_iface = await dbus_service.export(
            status_snapshot=self._status_snapshot,
            client_config=self._client_config,
            respond_permission=lambda request_id, decision: self._respond_permission(
                request_id, decision, "gnome"),
            respond_question=lambda request_id, answers: self._respond_question(
                request_id, answers, "gnome"),
            extend_request=self._extend_request,
            announce=self._announce_gnome,
            log=self._log,
        )

        async with self._websockets.serve(
            lambda ws, *args: self._handle_client(ws, secret, *args),
            "0.0.0.0",
            port,
        ):
            self._verbose_log(f"[ws] listening on :{port}")
            while not self._shutdown.is_set():
                await asyncio.sleep(2)
                if not self.clients and self.dbus_iface is None:
                    continue
                _, current_status, _, _ = self._overall_status()
                if current_status != self.last_status:
                    self.last_status = current_status
                    payload = self._status_snapshot()
                    msg = json.dumps(payload)
                    self._log(f"[ws] timeout → {current_status}")
                    if self.clients:
                        await asyncio.gather(
                            *[client.send(msg) for client in list(self.clients)],
                            return_exceptions=True,
                        )
                    self._emit_dbus("StatusChanged", msg)

    async def _handle_client(self, ws, secret: str, *_) -> None:
        if secret and not await self._authenticate(ws, secret):
            return

        self.clients.add(ws)
        self._log(f"[ws] client connected ({len(self.clients)} total)")
        try:
            await ws.send(json.dumps(self._status_snapshot()))

            client_name = "ws"
            try:
                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except Exception:
                        continue
                    client_name = await self._handle_client_message(
                        ws, message, client_name)
            except Exception:
                pass  # connection reset without close frame — normal on app restart
        finally:
            self.clients.discard(ws)
            self.permission_clients.discard(ws)
            self.conversation_clients.discard(ws)
            if ws in self.question_clients:
                self._note_question_client_gone()
            self.question_clients.discard(ws)
            self._log(f"[ws] client disconnected ({len(self.clients)} remaining)")

    async def _authenticate(self, ws, secret: str) -> bool:
        try:
            nonce = secrets.token_hex(16)
            await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(raw)
            if auth_core.valid_auth_response(data, secret, nonce):
                return True
            self._log(f"[ws] auth failed from {ws.remote_address}")
        except Exception:
            self._log(f"[ws] auth error from {ws.remote_address}")

        try:
            await ws.send(json.dumps({
                "error": "unauthorized",
                "message": "Wrong password",
            }))
        except Exception:
            pass
        await ws.close(1008, "Unauthorized")
        return False

    async def _handle_client_message(
        self,
        ws,
        message: JsonDict,
        client_name: str,
    ) -> str:
        message_type = message.get("type")
        if message_type == "subscribe":
            return await self._handle_subscribe(ws, message, client_name)

        if message_type == "permission_response":
            request_id = str(message.get("id", ""))
            decision = str(message.get("decision", ""))
            if self._respond_permission(request_id, decision, client_name):
                self._log(f"[perm] {decision} by {client_name}")
            return client_name

        if message_type == "question_response":
            request_id = str(message.get("id", ""))
            answers = message.get("answers")
            if self._respond_question(request_id, answers, client_name):
                self._log(f"[question] answered by {client_name}")
            return client_name

        if message_type == "extend":
            self._extend_request(str(message.get("id", "")))

        return client_name

    def _config_message(self, client: str) -> JsonDict:
        utc_offset = int(datetime.now().astimezone().utcoffset().total_seconds())
        return {
            "type": "config",
            "utc_offset": utc_offset,
            "remote_control": self._remote_permissions(),
            **self._client_config(client),
        }

    async def _handle_subscribe(
        self,
        ws,
        message: JsonDict,
        client_name: str,
    ) -> str:
        client_name = str(message.get("client") or "ws")
        features = message.get("features") or []

        # Config is tailored to the client type (e.g. the screen gets bitmap
        # logos), so it is sent in reply to the subscribe hello, not on connect.
        await ws.send(json.dumps(self._config_message(client_name)))

        wants_remote = (
            ("permissions" in features and self._remote_permissions())
            or ("questions" in features and self._remote_questions())
        )
        if wants_remote:
            self.permission_clients.add(ws)
            if "questions" in features and self._remote_questions():
                self.question_clients.add(ws)
            self._log(f"[ws] remote-control subscriber: {client_name}")
            for payload in self._pending_payloads():
                await ws.send(json.dumps(payload))

        if "conversation" in features and self._remote_permissions():
            self.conversation_clients.add(ws)
            self._log(f"[ws] conversation subscriber: {client_name}")
            snapshot = self._conversation_payload()
            if snapshot is not None:
                await ws.send(json.dumps(snapshot))
            self._notify_conversation_changed()

        return client_name

    def _emit_dbus(self, signal_name: str, message: str) -> None:
        if self.dbus_iface is None:
            return
        try:
            getattr(self.dbus_iface, signal_name)(message)
        except Exception:
            pass
