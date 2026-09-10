# Copyright 2026 The Plow Collective, Inc
# SPDX-License-Identifier: Apache-2.0
"""The agent's own email line as a Hermes platform.

A Gmail thread is a Plow chat with provider `gmail`, on the same grant and
the same socket as the phone line (design §1); this adapter serves those
and nothing else. Its identity -- platform name, session namespace, the
static hint -- is the registry entry `register` makes for it. Mail is
addressed to the agent, so there is no approval gate and no roster policy:
the agent writes as itself, from this line.
"""
import asyncio
import logging
import os

import aiohttp
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult

from ._transport import (
    BASE,
    _ACTIVE_TURN,
    _DIAGNOSTIC_PREFIXES,
    _bearer,
    _chat_type,
    _granted_chats,
    _owner_fact,
    _owner_identity,
    _self_agent_line,
    _serve,
    _socket,
    _split,
    _ticket,
)

PLATFORM_NAME = "plow_email"
PROVIDER = "gmail"
log = logging.getLogger(__name__)


def hint(address=None):
    """The platform hint (design §5). Static per platform, so the address is
    filled in once reach has read it -- see `_publish_hint`."""
    line = f"your own email line, {address}" if address else "your own email line"
    return f"This is {line}. Mail here is addressed to you; you write as yourself, at email length."


def check_requirements():
    return bool(os.environ.get("PLOW_AGENT_TOKEN"))


class PlowEmailAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        config.extra["group_sessions_per_user"] = False
        self.auth = _bearer()
        self.address = None                  # the line's address, off the first gmail chat
        self._chats = {}                     # uid -> chat resource, gmail only
        self._foreign = frozenset()          # the phone line's uids on the same grant
        self._seen_events = []
        self._ws_task = None

    @property
    def authorization_is_upstream(self):
        """Plow authenticated the sender and put them on the thread; Hermes
        must not pair on top. See PlowChatAdapter for the reasoning."""
        return True

    def _set_reach(self, listing):
        self._chats, self._foreign = _split(listing, PROVIDER)
        first = next(iter(self._chats.values()), None)
        address = _self_agent_line(first).get("provider_key") if first else None
        if address and address != self.address:
            self.address = address
            self._publish_hint()

    async def _refresh_reach(self, http):
        self._set_reach(await _granted_chats(http, self.auth))

    def _publish_hint(self):
        """The gateway reads `platform_registry.get(name).platform_hint` on
        every prompt build (agent/system_prompt.py `_platform_hint`), and the
        entry is a plain dataclass: writing the address onto it here is the
        whole mechanism. Imported lazily -- the registry is a runtime module
        the gateway supplies, like everything under `gateway.`."""
        from gateway.platform_registry import platform_registry
        platform_registry.get(PLATFORM_NAME).platform_hint = hint(self.address)

    async def get_chat_info(self, chat_id):
        chat = self._chats[chat_id]
        return {"name": chat.get("display_name") or chat_id, "type": _chat_type(chat), "chat_id": chat_id}

    async def connect(self, *, is_reconnect=False):
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        async with aiohttp.ClientSession() as http:
            await self._refresh_reach(http)
        self._ws_task = asyncio.create_task(self._listen())
        return True

    async def disconnect(self):
        if self._ws_task:
            self._ws_task.cancel()
        self._mark_disconnected()

    async def _listen(self):
        first_connection = True

        async def session(http):
            nonlocal first_connection
            if not first_connection:
                await self._refresh_reach(http)
            first_connection = False
            async with _socket(http, await _ticket(http, self.auth)) as ws:
                self._mark_connected()
                log.info("[plow_email] websocket connected")
                async for frame in ws:
                    if frame.type == aiohttp.WSMsgType.TEXT:
                        await self._on_frame(frame.json(), http)

        await _serve(session, self._mark_disconnected, PLATFORM_NAME)

    async def _on_frame(self, frame, http):
        if frame.get("type") == "connected":
            return
        chat_uid = frame["chat_id"]
        if chat_uid not in self._chats and chat_uid not in self._foreign:
            await self._refresh_reach(http)  # a thread born since the last read
        if chat_uid in self._foreign:
            return                           # the phone line's room; plow_chat's turn
        if chat_uid not in self._chats:
            log.warning("[plow_email] dropped frame outside the grant: %s", chat_uid)
            return
        if frame["event_type"] != "message_received" or frame["event_id"] in self._seen_events:
            return
        self._seen_events.append(frame["event_id"])
        del self._seen_events[:-512]
        await self._on_message(frame["data"]["message"], chat_uid)

    async def _on_message(self, msg, chat_uid):
        if msg["direction"] != "inbound":
            return                           # the echo of our own send
        sender = msg["sender"]
        if sender["type"] != "member":
            log.info("[plow_email] ignored sender.type=%r", sender["type"])
            return
        chat = self._chats[chat_uid]
        info = await self.get_chat_info(chat_uid)
        try:
            channel_prompt = _owner_fact(_owner_identity(chat))
        except RuntimeError as exc:
            # `_serve` logs the exception TYPE only -- an aiohttp handshake
            # error stringifies the ws URL, live ticket and all -- so the one
            # line naming the broken roster has to be logged here, and this
            # mail is already event-deduped: unlogged, it just disappears.
            log.error("[plow_email] %s", exc)
            raise
        await self.handle_message(MessageEvent(
            text=msg["body"].strip() or "(empty email)",
            source=self.build_source(chat_id=chat_uid, chat_name=info["name"], chat_type=info["type"],
                                     user_id=sender["uid"],
                                     user_name=sender.get("display_name") or sender["uid"],
                                     role_authorized=sender.get("role") == "owner"),
            message_id=msg["uid"],
            channel_prompt=channel_prompt,
        ))

    async def on_processing_start(self, event):
        # `dm` is False on purpose -- the Latch mail gate approves a plow-gog
        # send only in the owner's own DM, and a reply here goes out from this
        # line, never their Gmail.
        _ACTIVE_TURN.set({"chat_uid": event.source.chat_id,
                          "owner": bool(event.source.role_authorized), "dm": False})

    async def on_processing_complete(self, event, outcome):
        _ACTIVE_TURN.set(None)
