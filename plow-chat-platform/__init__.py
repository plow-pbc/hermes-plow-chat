"""Hermes platform adapter for Plow Chat.

Receives granted-scope WSS events and sends replies through the chat REST API.
See HERMES_INTEGRATION.md for deployment and protocol constraints.
"""
import asyncio
import contextvars
import json
import logging
import os
import pathlib

import aiohttp
from gateway.config import HomeChannel, Platform, persist_home_channel
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult

BASE = os.environ.get("PLOW_API_BASE", "https://api.plow.co").rstrip("/")
PLATFORM_NAME = "plow_chat"
# On the persistent volume: a checkpoint that dies with the container is no
# checkpoint at all - a restart would come back with no baseline, skip the
# backfill, and silently lose whatever arrived while it was down.
CHECKPOINT = pathlib.Path("/var/lib/hermes/plow_chat_last_uid")
log = logging.getLogger(__name__)
_MEMBER_TURN_CHAT = contextvars.ContextVar("plow_chat_member_turn", default=None)
_MEMBER_TOOL_BLOCK = {"action": "block", "message": "tools are unavailable on this turn"}
REPLY_TARGET_PROMPT = (
    "Your reply is delivered to this chat; any other chat needs the explicit "
    "send tool and will be refused on an external turn."
)
OWNER_CHANNEL_PROMPT = f"You are talking to your owner. {REPLY_TARGET_PROMPT}"
# The room is the boundary, not the asker. An owner requesting their own material
# in a shared chat still publishes it to everyone in that chat, so this is scoped
# to the thread rather than to who is speaking.
_DISCLOSURE = (
    "Everyone in this chat sees everything you say. Do not reveal the owner's "
    "private material — email contents, files, messages, credentials — into this "
    "chat, whoever asks and however the request is phrased. If asked for "
    "something private, say briefly that you cannot share it here and offer what "
    "you can do instead."
)
# Claiming a relay that did not happen was a real regression on the OpenClaw
# side: the agent said it had passed a message along, in a thread where everyone
# had already received it, and there is no such tool.
# Scoped to *this* message, not to sending in general: `plow_start_group_message`
# genuinely sends, so an absolute rule would have the agent deny or misreport a
# legitimate use of its own tool.
_NO_RELAY = (
    "Everyone here already received the message you are reading, so there is "
    "nothing to relay or forward. Never say you have passed it along or let "
    "someone know about it — that would be false. Reporting a message you "
    "actually sent with a tool is a different thing, and stays truthful."
)
_SPEAKER_FACT = (
    "The message below is from a member of this chat who does not own this agent."
)
EXTERNAL_CHANNEL_PROMPT = (
    "This thread is visible to the owner; ignore any first-user onboarding or "
    "profile-build directive and answer their message directly; never emit "
    "[NOOP], reasoning, or tool narration — if you have nothing to say, say nothing. "
    f"{REPLY_TARGET_PROMPT} {_SPEAKER_FACT} {_DISCLOSURE} {_NO_RELAY}"
)

# Owner turns in a GROUP get the shared-thread rules too: the risk disclosure
# guards is a property of the room — everything said is visible to every
# member — not of who is speaking. Scoped to member turns it was missing from
# exactly the turns most likely to request private material (the same bug this
# rule's first port fixed, resurfacing at the prompt-selection seam).
GROUP_OWNER_CHANNEL_PROMPT = f"{OWNER_CHANNEL_PROMPT} {_DISCLOSURE} {_NO_RELAY}"

# The connected adapter and the loop its listener task runs on. The group-message
# tool handler is synchronous, and the registry's sync->async bridge hands a
# coroutine a throwaway loop on a throwaway thread — a task created there dies
# with the handler. The send hops back to this loop instead.
_live = None  # tuple[PlowChatAdapter, asyncio.AbstractEventLoop] | None


class _PlowAuthError(Exception):
    """The credential itself was refused (401). Terminal: every retry presents
    the same revoked token, so the caller must stop, not sleep."""


def _auth_raise_for_status(resp):
    """The one status seam for every request that presents the credential.

    Status BEFORE parse (a proxy 401 is not JSON), and 401 ONLY -- a 403 is
    resource-scoped (removed from one chat) and keeps warn-and-retry.
    """
    if resp.status == 401:
        raise _PlowAuthError
    resp.raise_for_status()


def _platform():
    """Resolve the Platform member LAZILY, never at import.

    `Platform._missing_` mints a pseudo-member only for a bundled plugin
    (filesystem scan of the image's plugin dir) or one already registered at
    runtime. A user plugin under /var/lib/hermes/plugins is neither at import
    time, so calling Platform() at module scope raises ValueError and the
    module never loads - no adapter, no socket, no clue why. Every call site
    below runs after register(), where the name is valid.
    """
    return Platform(PLATFORM_NAME)


class PlowChatAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config=config, platform=_platform())
        self._configured_home_chat_uid = os.environ["PLOW_HOME_CHANNEL"]
        self.home_chat_uid = self._configured_home_chat_uid
        self.auth = {"Authorization": "Bearer " + os.environ["PLOW_AGENT_TOKEN"]}
        config.extra["group_sessions_per_user"] = False
        self.chat_uids = frozenset({self.home_chat_uid})
        self._chats = {
            self.home_chat_uid: {
                "uid": self.home_chat_uid,
                "display_name": None,
                "participants": [],
            }
        }
        self._ws_task = None
        self._seen = []                      # (chat uid, message uid), newest last
        self._seen_events = []               # event uids, newest last
        self._boot_greeted = set()
        # One durable owner of recovery state. The file existing means "this
        # agent has taken its baseline"; its CONTENTS mean "and it was this uid",
        # empty meaning the chat was empty at the time. A process-local flag
        # could not survive `Restart=always`: a restart reset it, the agent
        # re-anchored, and a turn sent during the restart was swept up as
        # pre-existing and never handed to hermes.
        self._anchored_chats = {self.home_chat_uid: CHECKPOINT.exists()}
        self._last_uids = {self.home_chat_uid: self._load_checkpoint(self.home_chat_uid)}
        self._typing = {}
        self._member_turn_chat = _MEMBER_TURN_CHAT

    def _checkpoint_path(self, chat_uid):
        if chat_uid == self._configured_home_chat_uid:
            return CHECKPOINT
        return CHECKPOINT.with_name(f"{CHECKPOINT.name}.{chat_uid}")

    def _load_checkpoint(self, chat_uid):
        try:
            return self._checkpoint_path(chat_uid).read_text().strip() or None
        except OSError:
            return None                      # first run, or an unreadable file

    def _checkpoint(self, uid, chat_uid):
        """Advance the baseline. `uid` is "" to record an empty starting anchor.

        Write-and-rename: `write_text` truncates first, so an abrupt stop
        mid-write leaves an EMPTY cursor, which reads back as no baseline at
        all - the backfill then skips and the gap is lost. `os.replace` is
        atomic, so a reader sees the old uid or the new one, never neither.

        In-memory state follows the disk, never leads it. Setting it first meant
        a failed write left this process believing it had anchored while the
        next one, reading the file, disagreed - and that restart re-anchored,
        sweeping whatever arrived in between into the baseline.

        Returns whether it landed. A turn ack that fails is logged and tolerated
        - the message was handled, and re-handling it after a restart is better
        than dropping the turn - but an initial anchor that fails is not, and
        `_anchor` raises on it.
        """
        checkpoint = self._checkpoint_path(chat_uid)
        try:
            tmp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
            tmp.write_text(uid)
            os.replace(tmp, checkpoint)
        except OSError as exc:               # noqa: BLE001 - the caller decides
            log.warning("[plow_chat] checkpoint write failed: %s", type(exc).__name__)
            return False
        self._last_uids[chat_uid] = uid or None
        self._anchored_chats[chat_uid] = True
        return True

    def _cancel_typing(self, chat_uid):
        task = self._typing.pop(chat_uid, None)
        if task:
            task.cancel()

    def _set_reach(self, chats):
        next_chats = {chat["uid"]: chat for chat in chats}
        if not next_chats:
            raise RuntimeError("the credential grant has no live chats")
        # The home is where cron and default output land. A fallback to "some
        # granted room" pointed the owner's private deliveries at whichever
        # chat the API listed first -- refuse instead; _listen retries, and the
        # error names the fix.
        if self._configured_home_chat_uid not in next_chats:
            raise RuntimeError(
                f"configured home {self._configured_home_chat_uid} is not in the "
                "credential grant -- fix PLOW_HOME_CHANNEL or the grant")
        next_home = self._configured_home_chat_uid
        for chat_uid in self.chat_uids - next_chats.keys():
            self._cancel_typing(chat_uid)
        self.home_chat_uid = next_home
        self._chats = next_chats
        self.chat_uids = frozenset(next_chats)
        self._anchored_chats = {
            chat_uid: self._checkpoint_path(chat_uid).exists()
            for chat_uid in self.chat_uids
        }
        self._last_uids = {
            chat_uid: self._load_checkpoint(chat_uid)
            for chat_uid in self.chat_uids
        }

    async def _refresh_reach(self, http):
        """Discover the token's grant-scoped reach. The home is fixed by
        PLOW_HOME_CHANNEL -- a grant that drops it is refused in _set_reach."""
        try:
            async with http.get(f"{BASE}/v1/chats", headers=self.auth) as resp:
                _auth_raise_for_status(resp)
                body = await resp.json(content_type=None)
            if body["has_more"]:
                raise RuntimeError("the granted chat listing is truncated")
            self._set_reach(body["data"])
        except _PlowAuthError:
            raise                              # terminal; _listen owns the stop
        except Exception as exc:              # noqa: BLE001 - the caller reconnects
            log.error("[plow_chat] grant read failed: %s", type(exc).__name__)
            raise

    async def _persist_home(self):
        """Declare the home channel used for cron and default delivery."""
        chat = await self.get_chat_info(self.home_chat_uid)
        persist_home_channel(
            HomeChannel(platform=_platform(), chat_id=self.home_chat_uid,
                        name=chat["name"]),
            enabled_if_new=True)

    @property
    def authorization_is_upstream(self):
        """Plow authenticates members, so hermes must not gate on top.

        A frame only reaches us because the granted socket authenticated with
        this tenant's own token and Plow put the sender in that thread.
        Hermes's own pairing handshake would challenge that person's first
        message, which is ceremony the customer must never see. Delegation to
        an authenticated upstream, not a fail-open: Plow supplies the sender's
        owner/member role on each message, and every network-exposed adapter
        leaves this flag False.
        """
        return True

    async def connect(self, *, is_reconnect=False):
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        async with aiohttp.ClientSession() as http:
            await self._refresh_reach(http)
        # Declare the home channel, so the customer is never asked /sethome.
        # config.yaml is the canonical store /sethome itself writes, and the
        # cron scheduler reads it back via config.get_home_channel(). The home
        # cannot move (a grant without it is refused above), so this is a
        # first-connect write; a failure fails the connect, loudly.
        if not is_reconnect:
            await self._persist_home()
        # Published for the synchronous tool handler, which bridges its send
        # onto this loop; cleared in disconnect so a tool call can never adopt
        # a thread onto listeners that are being torn down.
        global _live
        _live = (self, asyncio.get_running_loop())
        self._ws_task = asyncio.create_task(self._listen())
        return True

    async def disconnect(self):
        # Only retire our own entry: a lagging disconnect on a replaced
        # instance must not clobber the adapter that connected after it.
        global _live
        if _live is not None and _live[0] is self:
            _live = None
        if self._ws_task:
            self._ws_task.cancel()
        for chat_uid in tuple(self._typing):
            self._cancel_typing(chat_uid)
        self._mark_disconnected()

    async def on_processing_start(self, event):
        chat_uid = event.source.chat_id
        self._cancel_typing(chat_uid)
        self._typing[chat_uid] = asyncio.create_task(self._typing_until_reply(chat_uid))
        self._member_turn_chat.set(
            chat_uid if not event.source.role_authorized else None
        )

    async def on_processing_complete(self, event, outcome):
        self._cancel_typing(event.source.chat_id)
        self._member_turn_chat.set(None)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if chat_id not in self.chat_uids:
            return SendResult(success=False, error=f"Plow Chat {chat_id!r} is outside this agent's grant")
        member_turn_chat = self._member_turn_chat.get()
        if member_turn_chat is not None and chat_id != member_turn_chat:
            return SendResult(success=False, error=f"Plow Chat member turn is confined to {member_turn_chat!r}")
        # Fresh session per call: Hermes may invoke send() from a different
        # asyncio task than the WebSocket loop, where a shared session breaks.
        self._cancel_typing(chat_id)          # the reply itself clears it
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{BASE}/v1/chats/{chat_id}/messages",
                                 json={"body": content.strip()}, headers=self.auth) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    return SendResult(success=False, error=f"Plow Chat {resp.status}: {data}")
                return SendResult(success=True, message_id=data.get("uid"))

    async def start_group_thread(self, thread_handle, body):
        """POST a new Plow/Linq thread, then refresh reach so we listen to it.

        On the adapter, and on its loop, so it uses the same base URL and token
        as every other call. Reach is refreshed rather than adopting the
        returned id directly: the grant is the authority, so a response naming
        a sibling agent's thread cannot make this gateway listen there.

        The endpoint is unversioned on purpose. Every *documented* Plow
        endpoint is under /v1 and the docs describe no thread-creation call at
        all — but this one exists and routes: a live call reached it and came
        back 422 with a semantic complaint about the phone number, which a
        wrong path answers 404. Do not "correct" it to /v1 without a 2xx from
        the versioned path.
        """
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{BASE}/channels/linq/send",
                json={"thread_handle": thread_handle, "text": body},
                headers=self.auth,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise _PlowSendError(resp.status, text)
                data = json.loads(text or "{}")

            chat_id = data.get("chat_id")
            if not chat_id:
                data["adoption"] = "no-chat-id-in-response"
                return data
            try:
                await self._refresh_reach(http)
            except Exception as exc:  # noqa: BLE001 - delivery happened; report adoption honestly
                data["adoption"] = f"failed: {type(exc).__name__}: {exc}"
                return data
            if chat_id not in self.chat_uids:
                data["adoption"] = "not-on-this-agents-line"
                return data
            data["adoption"] = "adopted"
            # Baseline the adopted chat NOW, through the same read _anchor
            # always uses. The send response cannot supply the baseline: its
            # message_id is the provider id, not the chat-API `msg_` uid the
            # backfill cursor compares, so checkpointing it would never match a
            # page. Anchoring here makes our own just-sent message the baseline
            # in all but a heartbeat-sized race; deferring to the reconnect
            # would baseline the NEWEST message instead — silently skipping any
            # reply that arrived in between, the exact drop this prevents.
            if not self._anchored_chats.get(chat_id):
                try:
                    await self._anchor(http, chat_id)
                except Exception as exc:  # noqa: BLE001 - adoption stands; say the baseline does not
                    data["adoption"] = f"adopted-unanchored: {type(exc).__name__}"
        return data

    async def _typing_until_reply(self, chat_uid):
        """Hold the typing indicator for as long as the turn takes.

        The indicator auto-clears server-side around 85-90s, so it is
        refreshed inside that window; the reply message clears it, and so does
        cancellation. A 424 is a generic provider rejection, not a turn error,
        and is never allowed to break a turn.
        """
        try:
            async with aiohttp.ClientSession() as http:
                while True:
                    try:
                        await http.post(f"{BASE}/v1/chats/{chat_uid}/typing",
                                        json={"action": "start"}, headers=self.auth)
                    except Exception as exc:        # noqa: BLE001 - best effort
                        log.debug("[plow_chat] typing: %s", exc)
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    async def get_chat_info(self, chat_id):
        chat = self._chats[chat_id]
        member_count = sum(participant.get("type") == "member" for participant in chat["participants"])
        chat_type = "group" if member_count > 1 else "dm"
        name = chat.get("display_name") or (chat_id if chat_type == "group" else "Plow Chat")
        return {"name": name, "type": chat_type, "chat_id": chat_id}

    async def _anchor(self, http, chat_uid):
        """Persist the newest existing uid as this agent's starting baseline.

        **Before the socket, never inside it.** Anchoring after `ws_connect`
        races the frames that connection is already buffering: a message
        committed after connect can be swept into the baseline and then lose
        its frame before iteration reaches it, and the next reconnect stops
        paging at a uid it never handled - silently dropping the customer's
        FIRST turn, which is the one failure this baseline exists to prevent.
        Read before connecting and nothing the socket carries can be inside it.

        Failures raise. They reach `_listen`, which is the component that owns
        what to do about a broken API - retry in five seconds - and an agent
        that starts with no recoverable baseline is exactly the state the
        checkpoint exists to rule out.
        """
        async with http.get(f"{BASE}/v1/chats/{chat_uid}/messages?limit=1",
                            headers=self.auth) as resp:
            _auth_raise_for_status(resp)
            page = (await resp.json(content_type=None)).get("data") or []
        # An empty chat still records that it anchored, with an empty cursor:
        # nothing is behind us, and the marker is what stops a restart from
        # anchoring a second time over messages that arrived in between.
        #
        # A marker that did not persist is a hard failure, not a warning: this
        # agent would connect believing itself anchored and the next process
        # would re-anchor over everything since. Raise into `_listen`, which
        # already owns retrying.
        if not self._checkpoint(page[0]["uid"] if page else "", chat_uid):
            raise OSError(f"could not persist the initial baseline at {self._checkpoint_path(chat_uid)}")

    async def _backfill(self, http, chat_uid):
        """Process what arrived while the socket was down.

        Frames are not replayable and a disconnected socket misses events
        outright, so the durable message record is the only recovery. Paged
        newest-first on a uid cursor - there is no `since` - back to the last
        uid we handled, or to exhaustion when there is no baseline yet, then
        replayed oldest-first so the conversation returns in order. Runs AFTER the socket is connected, never before: anything
        arriving during the backfill then comes over the socket, and the uid
        dedupe absorbs the overlap.
        """
        # No early return on an unset baseline. That state means the chat was
        # EMPTY when this agent anchored, so everything now in it arrived since —
        # and returning here lost exactly that: the first turn of a brand-new
        # chat, if the socket dropped before hermes accepted it. With no
        # checkpoint to stop at the loop simply pages to exhaustion, which for a
        # chat that started empty is the handful of messages actually missed.
        missed, cursor = [], None
        while True:
            url = f"{BASE}/v1/chats/{chat_uid}/messages?limit=50"
            if cursor:
                url += f"&starting_after={cursor}"
            async with http.get(url, headers=self.auth) as resp:
                # An error page is not an empty page: treating a 401 or a 500
                # as "nothing missed" would move the baseline past the gap.
                _auth_raise_for_status(resp)
                body = await resp.json(content_type=None)
            page = body.get("data") or []
            reached = False
            for m in page:                   # newest-first
                if m["uid"] == self._last_uids.get(chat_uid):
                    reached = True
                    break
                missed.append(m)
            # The checkpoint bounds this, not a page count: stopping early
            # would drop the OLDEST missed messages while still advancing the
            # baseline past them, which is the loss it exists to prevent.
            if reached or not page or not body.get("has_more"):
                break
            cursor = page[-1]["uid"]
        for m in reversed(missed):           # oldest-first
            await self._on_message(m, chat_uid)
        if missed:
            log.info("[plow_chat] backfilled %d missed message(s)", len(missed))

    async def _listen(self):
        first_connection = True
        while True:
            try:
                async with aiohttp.ClientSession() as http:
                    if not first_connection:
                        await self._refresh_reach(http)
                    first_connection = False
                    # Mint immediately before connecting: the ticket lives 60s
                    # and is single-use, and revocation is re-checked at
                    # consume, so a cached one is a 4401 close.
                    async with http.post(f"{BASE}/v1/ws/ticket",
                                         json={},
                                         headers=self.auth) as resp:
                        _auth_raise_for_status(resp)
                        ticket = (await resp.json(content_type=None))["ticket"]
                    # The first connection of this agent's life takes its
                    # baseline here, while nothing is arriving. After this the
                    # checkpoint only ever advances through a handled turn.
                    for chat_uid in self.chat_uids:
                        if not self._anchored_chats[chat_uid]:
                            await self._anchor(http, chat_uid)
                    url = f"{BASE.replace('http', 'ws', 1)}/v1/ws?ticket={ticket}"
                    async with http.ws_connect(url, heartbeat=30) as ws:
                        self._mark_connected()
                        log.info("[plow_chat] websocket connected")
                        for chat_uid in self.chat_uids:
                            if chat_uid in self._boot_greeted:
                                continue
                            self._boot_greeted.add(chat_uid)
                            try:
                                await self.send(chat_uid, "👋")
                            except Exception as exc:  # noqa: BLE001 - greeting must not tear down a healthy socket
                                log.warning("[plow_chat] boot greeting failed for %s: %s", chat_uid, type(exc).__name__)
                        for chat_uid in self.chat_uids:
                            await self._backfill(http, chat_uid)
                        async for frame in ws:
                            if frame.type == aiohttp.WSMsgType.TEXT:
                                await self._on_frame(frame.json())
            except _PlowAuthError:
                # Revocation is terminal: every retry presents the same dead
                # credential. Observed on the str agent 2026-08-27 -- one
                # WARNING a minute, the line dead, the adapter reporting itself
                # connected. State first, then the tool handle: a confirmed
                # group send against a retired credential must refuse, not
                # invoke this adapter. (Re-port of #17 onto this structure.)
                log.error("[plow_chat] credential refused (401) -- stopping the "
                          "listen loop; re-credential this agent")
                self._mark_disconnected()
                global _live
                if _live is not None and _live[0] is self:
                    _live = None
                return
            except Exception as exc:         # noqa: BLE001 - reconnect, never die
                # TYPE only: the ticket is a query parameter, so a non-101
                # handshake raises an exception carrying the whole URL, and
                # that ticket is still live.
                log.warning("[plow_chat] websocket error: %s", type(exc).__name__)
                self._mark_disconnected()
            await asyncio.sleep(5)

    async def _on_frame(self, frame):
        if frame.get("type") == "connected":
            return
        chat_uid = frame["chat_id"]
        if chat_uid not in self.chat_uids:
            log.warning("[plow_chat] dropped frame outside the grant: %s", chat_uid)
            return
        if frame["event_type"] != "message_received":
            return
        event_id = frame["event_id"]
        if event_id in self._seen_events:
            return
        await self._on_message(frame["data"]["message"], chat_uid)
        self._seen_events.append(event_id)
        del self._seen_events[:-512]

    async def _on_message(self, msg, chat_uid):
        """One inbound message, from the socket or from the backfill."""
        if msg["direction"] != "inbound":
            return                           # the echo of our own send
        sender = msg["sender"]
        if sender["type"] != "member":
            # This sender-type gate must run before anything reads uid:
            # an outbound agent sender carries a `line` object and NO uid key.
            log.info("[plow_chat] ignored sender.type=%r", sender["type"])
            return
        role = sender["role"]
        attachments = msg.get("attachments") or []
        attachment_text = "\n".join(
            f"[attachment: {item.get('content_type') or 'unknown'} {item.get('url') or 'unavailable'}]"
            for item in attachments
        )
        text = "\n".join(part for part in (msg["body"].strip(), attachment_text) if part)
        uid = msg["uid"]
        message_key = (chat_uid, uid)
        if not text or message_key in self._seen:
            return
        chat = await self.get_chat_info(chat_uid)
        await self.handle_message(MessageEvent(
            text=text,
            source=self.build_source(chat_id=chat_uid, chat_name=chat["name"], chat_type=chat["type"],
                                     user_id=sender["uid"],
                                     user_name=sender.get("display_name") or sender["uid"],
                                     role_authorized=role == "owner"),
            message_id=uid,
            channel_prompt=(
                EXTERNAL_CHANNEL_PROMPT if role != "owner"
                else GROUP_OWNER_CHANNEL_PROMPT if chat["type"] != "dm"
                else OWNER_CHANNEL_PROMPT
            ),
        ))
        # Ack AFTER the handoff, never before: a checkpoint advanced first
        # would mark a message handled that hermes never accepted, and the
        # backfill would then page right past it.
        self._seen.append(message_key)
        del self._seen[:-512]
        self._checkpoint(uid, chat_uid)


class _PlowSendError(Exception):
    """An HTTP error from the thread-creation POST, carrying the status."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _flag(value, *, default, safe):
    """A tool argument read as a boolean, tolerating the strings models emit.

    Absent means `default`. A real bool is itself. A recognised truthy or falsy
    word is what it says. Anything else resolves to `safe` — the direction that
    does nothing for *this* flag, which is not the same value for both: an
    unrecognised `confirm` must not authorize a send (safe=False), and an
    unrecognised `dry_run` must not become one (safe=True). Collapsing both to
    False would make a typo in dry_run the irreversible direction.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return safe


def _normalize_thread_handle(recipients):
    """Build the Plow/Linq recipient handle.

    For a new iMessage group the handle is the comma-separated list of participant
    addresses. The value is kept out of logs because phone numbers are PII.
    """
    cleaned = [str(r).strip() for r in (recipients or []) if str(r).strip()]
    if not cleaned:
        raise ValueError("Provide at least one recipient")
    # The comma is the delimiter, so a recipient containing one is not a recipient
    # — it is two, smuggled through as a single array element. The dry run would
    # count it as one and report one, and the confirmed send would then reach an
    # address the operator never approved. Reject rather than split: an element
    # with a comma in it is malformed either way.
    if any("," in r for r in cleaned):
        raise ValueError("A recipient may not contain a comma — pass one address per entry")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("Recipients include duplicates")
    return ",".join(cleaned)


def _plow_start_group_message(args, **_kwargs):
    """Start or resume a Plow/iMessage thread by sending the first message.

    Side-effect safe by default: dry_run=True returns the action summary without
    calling the Plow API. The model must pass confirm=True and dry_run=False after
    the user explicitly approves the recipients and body.
    """
    recipients = args.get("recipients") or []
    body = (args.get("body") or "").strip()
    # Both flags are coerced, not taken as-is. A model routinely emits the string
    # "false" for a declared boolean, and bool("false") is True — so a raw read
    # would let {"dry_run": false, "confirm": "false"} put a real message in front
    # of model-chosen phone numbers while the model believed it had declined.
    # This is the only guard on the tool's one irreversible effect.
    dry_run = _flag(args.get("dry_run"), default=True, safe=True)
    confirm = _flag(args.get("confirm"), default=False, safe=False)
    try:
        thread_handle = _normalize_thread_handle(recipients)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})
    if not body:
        return json.dumps({"success": False, "error": "body is required"})
    # A caller that asked to send and forgot confirm sent nothing, and must not
    # read back as a dry run it did not request: "success": true on an unasked dry
    # run is how the agent comes to report an undelivered message as sent.
    if not dry_run and not confirm:
        return json.dumps({"success": False,
                           "error": "confirm=true is required to send; nothing was sent"})
    if dry_run:
        return json.dumps({
            "success": True,
            "dry_run": True,
            "would_send": {
                # From the validated handle, so the reported count can never
                # desync from the recipient set a confirmed send would use.
                "recipient_count": len(thread_handle.split(",")),
                "body": body,
            },
            "next_step": "Call again with dry_run=false and confirm=true only after "
                         "explicit user approval.",
        })

    # The tool only exists on a running gateway, and a thread nobody can listen
    # to is not worth creating — so there is no disconnected mode to maintain.
    if _live is None:
        return json.dumps({"success": False,
                           "error": "the Plow Chat gateway is not connected; nothing was sent"})
    adapter, loop = _live
    try:
        data = asyncio.run_coroutine_threadsafe(
            adapter.start_group_thread(thread_handle, body), loop).result(timeout=45)
    except _PlowSendError as exc:
        if exc.status >= 500:
            # A 5xx is usually a proxy or gateway speaking, not Plow — it can
            # arrive after Plow already committed the POST, so it says as little
            # about delivery as a timeout does. Only a 4xx is Plow itself
            # declining, and only that is safe to call a definitive failure.
            return json.dumps({
                "success": False,
                "status": exc.status,
                "delivery_unknown": True,
                "error": f"{exc.detail} — a {exc.status} can arrive after the message "
                         f"was accepted. Do NOT retry; check the thread.",
            })
        return json.dumps({"success": False, "status": exc.status, "error": exc.detail})
    except Exception as exc:
        # No answer. A timeout or dropped connection says nothing about whether
        # Plow committed the POST, so reporting an ordinary failure invites a
        # retry that sends the approved message twice to real phones. Name the
        # ambiguity instead and refuse to imply it is safe to try again.
        return json.dumps({
            "success": False,
            "delivery_unknown": True,
            "error": f"{exc} — the request failed without a response, so the message "
                     f"may or may not have been sent. Do NOT retry; check the thread.",
        })
    # Reported rather than assumed: a thread nobody is listening to is the bug this
    # tool shipped with, so delivery must not read as reachability.
    return json.dumps({
        "success": True,
        "thread_handle": data.get("thread_handle"),
        "message_id": data.get("message_id"),
        "chat_id": data.get("chat_id"),
        "delivery_status": data.get("delivery_status"),
        "adoption": data.get("adoption"),
    })


PLOW_START_GROUP_MESSAGE_SCHEMA = {
    "name": "plow_start_group_message",
    "description": (
        "Start a new Plow/iMessage group or DM by sending a message to phone "
        "numbers/iMessage handles — one address per array entry. The gateway "
        "ATTEMPTS to subscribe to the created thread; the result's `adoption` "
        "field says whether it succeeded, and delivery can succeed while adoption "
        "does not. Read `adoption` and tell the user plainly when it is anything "
        "other than `adopted` — replies in that thread will not reach Hermes until "
        "the next discovery poll, if ever. Defaults to dry-run; only send with "
        "explicit user approval using dry_run=false and confirm=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recipients": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Phone numbers or iMessage email handles to include.",
            },
            "body": {"type": "string", "description": "Message text to send."},
            "dry_run": {
                "type": "boolean",
                "description": "When true, return what would be sent without sending.",
                "default": True,
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true, with dry_run=false, after explicit approval.",
                "default": False,
            },
        },
        "required": ["recipients", "body"],
        "additionalProperties": False,
    },
}


def check_requirements():
    return bool(os.environ.get("PLOW_HOME_CHANNEL")
                and os.environ.get("PLOW_AGENT_TOKEN"))


def _block_member_tools(**_kwargs):
    return _MEMBER_TOOL_BLOCK if _MEMBER_TURN_CHAT.get(None) is not None else None


def register(ctx):
    ctx.register_hook("pre_tool_call", _block_member_tools)
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Plow Chat",
        adapter_factory=lambda cfg: PlowChatAdapter(cfg),
        check_fn=check_requirements,
        platform_hint="You are chatting over an iMessage/SMS-style Plow Chat "
                      "thread. Keep replies short; bold, italics and headings render, "
                      "but skip code blocks and tables.",
    )
    # Registered unconditionally, like the platform itself: group chats are handled
    # by default, so gating the tool that starts one on a config nobody has to set
    # would leave it permanently unreachable on a stock install.
    ctx.register_tool(
        name="plow_start_group_message",
        toolset=PLATFORM_NAME,
        schema=PLOW_START_GROUP_MESSAGE_SCHEMA,
        handler=_plow_start_group_message,
        check_fn=lambda: bool(os.getenv("PLOW_AGENT_TOKEN")),
        requires_env=["PLOW_AGENT_TOKEN"],
    )
