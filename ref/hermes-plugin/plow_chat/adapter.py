"""
Reference Plow Chat platform adapter for Hermes Agent.

This is intentionally a small, readable seed implementation. It documents the
shape of a Hermes platform adapter backed by the Plow Chat API. Production
installs should add durable cursor persistence, richer setup UX, and tests
against the exact Hermes version they run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

DEFAULT_BASE_URL = "https://api.plow.co"
MAX_MESSAGE_LENGTH = 4_000
DEFAULT_WELCOME_MESSAGE = "Hi — Plow Chat is connected to Hermes now. Reply here to start chatting."


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _base_url_from_env_or_config(config) -> str:
    extra = getattr(config, "extra", {}) or {}
    return (os.getenv("PLOW_CHAT_BASE_URL") or extra.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def _chat_uid_from_env_or_config(config) -> str:
    extra = getattr(config, "extra", {}) or {}
    return (os.getenv("PLOW_CHAT_CHAT_UID") or extra.get("chat_uid") or "").strip()


def _token_from_env_or_config(config) -> str:
    # Prefer env. Keeping the token in config.extra works for local experiments
    # but is not recommended because config files are easier to accidentally
    # commit.
    extra = getattr(config, "extra", {}) or {}
    return (os.getenv("PLOW_CHAT_TOKEN") or extra.get("token") or "").strip()


def _ws_url_for(base_url: str, ticket: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/v1/ws?ticket={ticket}"


def _flatten_message(content: str) -> str:
    # Keep this deliberately conservative. iMessage/SMS render Markdown as text.
    return str(content or "").strip()


def _welcome_message_from_env() -> str:
    return os.getenv("PLOW_CHAT_WELCOME_MESSAGE", DEFAULT_WELCOME_MESSAGE).strip()


def _auto_welcome_enabled() -> bool:
    return str(os.getenv("PLOW_CHAT_AUTO_WELCOME", "true")).strip().lower() not in {"0", "false", "no", "off"}


def _auto_approve_enabled() -> bool:
    return str(os.getenv("PLOW_CHAT_AUTO_APPROVE_PAIRING", "true")).strip().lower() not in {"0", "false", "no", "off"}


class PlowChatAdapter(BasePlatformAdapter):
    """Plow Chat <-> Hermes gateway adapter."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("plow_chat"))
        self.base_url = _base_url_from_env_or_config(config)
        self.chat_uid = _chat_uid_from_env_or_config(config)
        self.token = _token_from_env_or_config(config)
        self._http_session = None
        self._ws_task: Optional[asyncio.Task] = None
        self._seen_message_uids: set[str] = set()
        self._stop_event = asyncio.Event()
        self._welcome_sent = False

    @property
    def name(self) -> str:
        return "Plow Chat"

    async def connect(self) -> bool:
        import aiohttp

        if not self.chat_uid or not self.token:
            msg = "PLOW_CHAT_CHAT_UID and PLOW_CHAT_TOKEN are required"
            logger.error("[plow_chat] %s", msg)
            self._set_fatal_error("config_missing", msg, retryable=False)
            return False

        # NOTE: shared session retained for the WSS loop only. The send() method
        # below creates a per-call session to avoid cross-event-loop "Timeout
        # context manager should be used inside a task" errors when the boss
        # skill invokes send_message from a tool-call context.
        self._http_session = aiohttp.ClientSession()
        self._stop_event.clear()
        self._ws_task = asyncio.create_task(self._websocket_loop())
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        import aiohttp

        target_chat = chat_id or self.chat_uid
        if target_chat != self.chat_uid:
            return SendResult(success=False, error="This seed adapter is configured for one Plow chat")

        body = _flatten_message(content)
        if not body:
            return SendResult(success=False, error="empty message")

        chunks = self.truncate_message(body)
        # Per-call session. Sharing self._http_session across calls works for the
        # WSS loop (same event loop) but fails when send() is invoked from the
        # boss skill's tool-call context (different asyncio task), producing
        # "Timeout context manager should be used inside a task" errors. The cost
        # of a fresh session per send is negligible (~1ms) and eliminates the bug.
        session = aiohttp.ClientSession()
        close_session = True
        last_message_id = None
        try:
            for chunk in chunks:
                async with session.post(
                    f"{self.base_url}/v1/chats/{self.chat_uid}/messages",
                    json={"body": chunk},
                    headers={"Authorization": f"Bearer {self.token}"},
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        err = data.get("error", {}) if isinstance(data, dict) else {}
                        code = err.get("code") or resp.status
                        msg = err.get("message") or str(data)
                        return SendResult(success=False, error=f"Plow Chat {code}: {msg}")
                    last_message_id = data.get("uid") if isinstance(data, dict) else None
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.warning("[plow_chat] send failed: %s", exc)
            return SendResult(success=False, error=str(exc))
        finally:
            if close_session:
                await session.close()

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # Plow Chat currently exposes no typing endpoint.
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": "Plow Chat", "type": "dm"}

    async def _mint_ws_ticket(self) -> str:
        async with self._http_session.post(
            f"{self.base_url}/v1/ws/ticket",
            json={"chat_id": self.chat_uid},
            headers={"Authorization": f"Bearer {self.token}"},
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                err = data.get("error", {}) if isinstance(data, dict) else {}
                raise RuntimeError(err.get("message") or f"ticket mint failed: {resp.status}")
            return data["ticket"]

    async def _websocket_loop(self) -> None:
        import aiohttp

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                ticket = await self._mint_ws_ticket()
                ws_url = _ws_url_for(self.base_url, ticket)
                async with self._http_session.ws_connect(ws_url, heartbeat=30) as ws:
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_frame(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[plow_chat] websocket loop error: %s", exc)
                if self.is_connected:
                    self._mark_disconnected()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle_ws_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "connected":
            self._mark_connected()
            logger.info("[plow_chat] websocket subscribed")
            return
        if frame_type == "chat_active":
            logger.info("[plow_chat] chat active")
            await self._send_activation_welcome()
            return
        if frame_type == "participant_verified":
            self._approve_sender_from_frame(frame)
            return
        if frame_type == "chat_activation_failed":
            reason = frame.get("reason", "activation_failed")
            message = frame.get("message") or reason
            self._set_fatal_error("chat_activation_failed", message, retryable=False)
            await self._notify_fatal_error()
            return
        if frame_type == "message_status_updated":
            logger.debug("[plow_chat] status update: %s", frame.get("message"))
            return
        if frame_type != "message_received":
            return

        message = frame.get("message") or {}
        if message.get("direction") != "inbound":
            return
        msg_uid = message.get("uid") or str(int(time.time() * 1000))
        if msg_uid in self._seen_message_uids:
            return
        self._seen_message_uids.add(msg_uid)

        sender = message.get("sender") or {}
        user_id = sender.get("uid") or sender.get("provider_key") or "member"
        user_name = sender.get("display_name") or user_id
        text = message.get("body") or ""
        if not text.strip():
            return

        self._approve_plow_member(user_id, user_name)

        source = self.build_source(
            chat_id=self.chat_uid,
            chat_name="Plow Chat",
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=frame,
            message_id=msg_uid,
        )
        await self.handle_message(event)

    async def _send_activation_welcome(self) -> None:
        """Send one setup-success message when Plow reports the chat active.

        The WebSocket can be connected while the chat is still pending; Plow
        emits ``chat_active`` after the user texts the verification code, at
        which point the user gets an immediate setup confirmation.
        """
        if self._welcome_sent or not _auto_welcome_enabled():
            return
        message = _welcome_message_from_env()
        if not message:
            return
        # Latch before sending: a welcome POST can commit server-side even when
        # the client observes a failure, so we attempt it at most once and a
        # duplicate ``chat_active`` frame does not re-send.
        self._welcome_sent = True
        result = await self.send(self.chat_uid, message)
        if not result.success:
            logger.warning("[plow_chat] activation welcome send failed: %s", result.error)

    def _approve_sender_from_frame(self, frame: dict[str, Any]) -> None:
        """Best-effort approval from activation/verification frames."""
        candidates = []
        for key in ("participant", "member", "sender"):
            value = frame.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        chat = frame.get("chat")
        if isinstance(chat, dict):
            participants = chat.get("participants") or []
            candidates.extend(p for p in participants if isinstance(p, dict))
        for item in candidates:
            if item.get("type") in {None, "member"}:
                user_id = item.get("uid") or item.get("provider_key")
                if user_id:
                    self._approve_plow_member(user_id, item.get("display_name") or user_id)

    def _approve_plow_member(self, user_id: str, user_name: str = "") -> None:
        """Best-effort DM pairing approval for the verified Plow member.

        Plow already gates this chat by verification and Bearer auth. Hermes
        pairing is an additional generic gateway layer; approving the member uid
        here prevents the first real user message from being replaced by an
        unrelated pairing-code prompt.
        """
        if not (_auto_approve_enabled() and user_id):
            return
        try:
            from gateway.pairing import PairingStore
        except Exception:
            logger.debug("[plow_chat] PairingStore unavailable; skipping auto-approval", exc_info=True)
            return
        try:
            store = PairingStore()
            if hasattr(store, "approve_user"):
                store.approve_user("plow_chat", user_id, user_name)
                return
            with store._lock:
                store._approve_user("plow_chat", user_id, user_name)
        except Exception:
            logger.debug("[plow_chat] pairing auto-approval failed", exc_info=True)


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("PLOW_CHAT_CHAT_UID") and os.getenv("PLOW_CHAT_TOKEN"))


def validate_config(config) -> bool:
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(_chat_uid_from_env_or_config(config) and _token_from_env_or_config(config))


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    chat_uid = os.getenv("PLOW_CHAT_CHAT_UID", "").strip()
    token = os.getenv("PLOW_CHAT_TOKEN", "").strip()
    if not (chat_uid and token):
        return None
    seed = {
        "base_url": os.getenv("PLOW_CHAT_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        "chat_uid": chat_uid,
    }
    home = os.getenv("PLOW_CHAT_HOME_CHANNEL", "").strip() or chat_uid
    seed["home_channel"] = {"chat_id": home, "name": "Plow Chat"}
    return seed


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id=None, media_files=None, force_document=False) -> dict:
    adapter = PlowChatAdapter(pconfig)
    target_chat = chat_id or adapter.chat_uid
    result = await adapter.send(target_chat, message)
    if result.success:
        return {"success": True, "message_id": result.message_id}
    return {"error": result.error or "send failed"}


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system."""
    ctx.register_platform(
        name="plow_chat",
        label="Plow Chat",
        adapter_factory=lambda cfg: PlowChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["PLOW_CHAT_CHAT_UID", "PLOW_CHAT_TOKEN"],
        install_hint="Create and verify a Plow chat, then set PLOW_CHAT_* in Hermes data/.env",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="PLOW_CHAT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        pii_safe=True,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Plow Chat over an iMessage/SMS-style thread. "
            "Use concise plain text. Avoid relying on rich markdown rendering."
        ),
    )
