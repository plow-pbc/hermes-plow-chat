"""
The Plow Chat platform adapter for Hermes Agent.

This is the module Hermes loads from an installed plugin directory, and it is
what `agent-mgr` installs into every agent in the fleet -- it is the phone line,
not a sketch of one. Inbound arrives on a WebSocket this dials out on; outbound
and cron delivery go back through the chat REST API. Covered by tests/.

One known gap, tracked rather than hidden: there is no persisted checkpoint and
no history backfill, so turns that arrive while the socket is down are not
recovered. See issue #2 in this repo -- plow's own tenant adapter has the solved
form.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

DEFAULT_BASE_URL = "https://api.plow.co"
MAX_MESSAGE_LENGTH = 4_000
DEFAULT_WELCOME_MESSAGE = "Hi — Plow Chat is connected to Hermes now. Reply here to start chatting."


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


# The ticket is a live credential and it travels in the URL above, which is the
# one place it can reach a log: aiohttp renders the full request URL into
# WSServerHandshakeError, so logging that exception writes the ticket out.
# Measured against the real client — a refused handshake renders
#   403, message='Invalid response status', url='wss://.../v1/ws?ticket=...'
# while a DNS failure names only the host, so the leak is specific to the
# handshake path rather than to error logging in general.
_TICKET_IN_TEXT = re.compile(r"(ticket=)[^&\s'\"]+")


def _redact_ticket(text: str) -> str:
    """Mask any ws ticket that reached a string bound for a log.

    Substitution rather than dropping the message: the status code and reason
    are what make a handshake failure diagnosable, and they carry no secret.
    """
    return _TICKET_IN_TEXT.sub(r"\1<redacted>", text)


def _flatten_message(content: str) -> str:
    # Keep this deliberately conservative. iMessage/SMS render Markdown as text.
    return str(content or "").strip()


def _welcome_message_from_env() -> str:
    return os.getenv("PLOW_CHAT_WELCOME_MESSAGE", DEFAULT_WELCOME_MESSAGE).strip()


def _auto_welcome_enabled() -> bool:
    return str(os.getenv("PLOW_CHAT_AUTO_WELCOME", "true")).strip().lower() not in {"0", "false", "no", "off"}


def _auto_approve_enabled() -> bool:
    return str(os.getenv("PLOW_CHAT_AUTO_APPROVE_PAIRING", "true")).strip().lower() not in {"0", "false", "no", "off"}


try:
    from gateway.response_filters import SILENT_REPLY_TOKEN
except ImportError:
    # Older gateways only, and narrow on purpose. GROUP_POLICY bakes this token in
    # at import time, so falling back to a token the live response filter does not
    # strip is user-visible in exactly the case the policy exists to prevent: the
    # agent posts a literal marker into every group it meant to stay quiet in. A
    # module that exists but fails to import should raise, not degrade.
    SILENT_REPLY_TOKEN = "[SILENT]"
    logger.warning("[plow_chat] gateway.response_filters.SILENT_REPLY_TOKEN not "
                   "importable; group silence falls back to %r", SILENT_REPLY_TOKEN)

class _PlowSendError(Exception):
    """An HTTP error from the thread-creation POST, carrying the status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


_ADDRESSED_ONLY = (
    "This is a shared group chat. Respond only when you reasonably infer, "
    "as a human participant would, that Hermes is directly or contextually "
    "addressed or that a response is clearly expected. Otherwise output "
    f"exactly {SILENT_REPLY_TOKEN} and no other text."
)

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

# Composed from named blocks so a test can assert which rules a turn carries
# without scanning the prose for keywords, which passes on a rewrite that
# inverts the meaning.
GROUP_POLICY = "\n\n".join([_ADDRESSED_ONLY, _DISCLOSURE, _NO_RELAY])

# How long a thread can sit unheard after the operator adds Hermes to it. One
# call a minute against a list that changes a few times a year; the cost of
# shortening it is quota, of lengthening it somebody repeating themselves.
RECONCILE_SECONDS = 60

# The connected adapter and the loop its listener tasks run on. The group-message
# tool handler is synchronous, and the registry's sync->async bridge hands a
# coroutine a throwaway loop on a throwaway thread — a task created there dies
# with the handler. Adoption hops back to this loop instead.
_live: "tuple[PlowChatAdapter, asyncio.AbstractEventLoop] | None" = None


def _groups(extra: dict, home_chat_uid: str) -> dict[str, dict]:
    """uid -> {"name", "prompt"}, from PLOW_CHAT_GROUP_UIDS and extra.group_prompts.

    Two kinds of fact, so two files. Which chats exist and what they are called is
    account-specific activation state, and sits in the dotenv beside
    PLOW_CHAT_CHAT_UID. What a group is *for* is declarative, and is keyed by
    display name in config — which therefore names no cht_ id, and survives a
    restore onto an account whose uids are all different.
    """
    groups: dict[str, dict] = {}
    for entry in os.getenv("PLOW_CHAT_GROUP_UIDS", "").split(","):
        if not entry.strip():
            continue
        uid, sep, name = entry.partition("=")
        uid, name = uid.strip(), name.strip()
        # Errors name the offending entry: with several groups configured, the
        # rule alone leaves the operator guessing which one broke it, mid
        # crash-loop. Chat ids and display names are not secret; the dotenv's
        # credentials are on other lines.
        if not sep or not name:
            raise ValueError(
                f"PLOW_CHAT_GROUP_UIDS entry {entry.strip()!r} must be "
                "'<cht_ id>=<display name>'"
            )
        if not uid.startswith("cht_") or uid == home_chat_uid:
            raise ValueError(
                f"PLOW_CHAT_GROUP_UIDS entry {uid!r} must be a group cht_ ID and "
                "must not be PLOW_CHAT_CHAT_UID"
            )
        if uid in groups:
            raise ValueError(f"PLOW_CHAT_GROUP_UIDS repeats chat id {uid!r}")
        groups[uid] = {"name": name, "prompt": None}

    prompts = (extra or {}).get("group_prompts") or {}
    named = {group["name"]: group for group in groups.values()}
    # Two groups sharing a name defeats the point: the agent cannot tell them
    # apart, a send cannot resolve one, and a prompt keyed by that name would
    # silently reach only whichever was listed last.
    if len(named) != len(groups):
        names = [group["name"] for group in groups.values()]
        repeated = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"PLOW_CHAT_GROUP_UIDS repeats display name(s) {repeated}")
    # Warned rather than raised: config carries the prompts while the labels live
    # in the untracked dotenv, so an orphan is the *normal* state on a fresh
    # restore. Raising would brick the gateway in the recovery path config exists
    # to serve.
    orphaned = sorted(set(prompts) - set(named))
    if orphaned:
        logger.warning("[plow_chat] group_prompts names no configured group: %s", orphaned)
    for name, prompt in prompts.items():
        if name in named:
            named[name]["prompt"] = (prompt or "").strip() or None
    return groups


def _is_owner(participant: dict) -> bool:
    """Whether a sender or roster participant is the operator who owns this agent.

    The single owner of that fact. `role` is the Chat API's own answer
    (plow-pbc/plow#1381), served on both surfaces this adapter reads — message
    senders and chat participants. Re-deriving it by comparing handles is the
    drift `role` exists to retire: the roster and the wire spell the same person
    differently, and the disagreement is silent in both directions.

    Load-bearing: it is resolved against the handles the *account* holds, not
    per-chat, so a room's creator or admin is not an owner. A per-chat `role` on
    any surface would break this, because vouching trusts the field in rooms the
    operator does not own.

    An absent `role` reads as not-owner: absent data must never elevate. That is
    also what makes this total over the agent's own traffic, which carries none.
    """
    return participant.get("role") == "owner"


def _speaker_line(sender: dict) -> str:
    """Whether the speaker owns this agent — deliberately not who they are.

    The name is already on the event as `source.user_name`, normalized once in
    `_dispatch`. Repeating it here would be a second identity seam with its own
    fallbacks, and it would put provider-supplied text into the channel prompt,
    which the model weighs above the message body. Today only a handle could
    arrive — Plow sets a participant's `display_name` to the handle because Linq
    has no name field — but names are a planned addition, and this is the line
    they would be injected into. So the prompt carries the fact and the event
    carries the name.
    """
    if _is_owner(sender):
        return "The message below is from the owner of this agent."
    return ("The message below is from a member of this chat who does not own "
            "this agent.")


def _channel_prompt(group: dict | None, sender: dict) -> str:
    """GROUP_POLICY always leads; a configured group's prompt appends, never replaces.

    An adopted chat has no configured prompt, so it gets the bare policy — it is a
    room Hermes was added to, not one the operator described.

    The speaker line goes last, closest to the message it describes: the policy
    and the group's prompt are properties of the room and the same every turn,
    while this changes with each one.
    """
    parts = [GROUP_POLICY]
    if group is not None and group["prompt"]:
        parts.append(group["prompt"])
    parts.append(_speaker_line(sender))
    return "\n\n".join(parts)


async def _body(resp):
    """The JSON body, or the failure in the server's own words.

    Status before parsing. A Plow error carries `detail` and none of the keys the
    happy path reads, so indexing first renames the cause after whichever key is
    missing — a revoked token surfacing as `'ticket'`, saying nothing about the
    credential. And an error from something in front of Plow (a proxy 502, a WAF
    429) is not JSON at all, so decoding first loses the status to a decode error.
    """
    if resp.status >= 400:
        raise RuntimeError(f"Plow {resp.status}: {(await resp.text())[:200]}")
    return await resp.json(content_type=None)


def _members(chat: dict) -> list[dict]:
    """The people in a chat. The remaining participant is this agent's own line.

    .get-based like _agent_line: both read the same uncontrolled user-wide
    listing, and tolerating a shape in one place while raising on it in the other
    just moves the sweep-abort one line down.
    """
    return [m for m in (chat.get("participants") or []) if m.get("type") == "member"]


def _agent_line(chat: dict) -> str | None:
    """Which Plow line this chat is on, or None if the chat cannot be classified.

    One account runs several agents — a line each — and GET /v1/chats returns all
    of their chats together. Ours is whichever line the home chat is on; the rest
    belong to a sibling agent and are not ours to answer in.

    None rather than an exception: this runs over a user-wide listing we do not
    control the shape of, and one unrecognised entry must not abort the sweep that
    discovers every other chat.
    """
    for member in chat.get("participants") or []:
        if member.get("type") == "agent":
            line = member.get("line") or {}
            return line.get("uid")
    return None


class PlowChatAdapter(BasePlatformAdapter):
    """Plow Chat <-> Hermes gateway adapter."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("plow_chat"))
        self.base_url = _base_url_from_env_or_config(config)
        self.chat_uid = _chat_uid_from_env_or_config(config)
        self.token = _token_from_env_or_config(config)
        extra = getattr(config, "extra", None)
        writable = isinstance(extra, dict)
        if not writable:
            extra = {}
        self.groups = _groups(extra, self.chat_uid)
        # Reach starts at the home chat and nothing else — including configured
        # groups. A dotenv entry is a claim about a chat, not proof of one: until
        # the poll has seen it on *this* agent's line it must not be subscribed,
        # or a stale entry naming a sibling agent's chat receives our traffic.
        self.chat_uids = frozenset({self.chat_uid})
        # Only operator vouches are stored. Configured authority is *derived* from
        # groups + validated reach, because the two have different lifetimes: a
        # vouch dies with the operator that made it, a configured grant does not.
        # One set holding both forced a subtract-and-re-add on every transition.
        self.operator_vouched: set[str] = set()
        # The owner's handle, learned off the home chat by the poll. Its only job is
        # to notice an operator *change*; whether any given message is the operator's
        # is `_is_owner`'s answer, never a comparison against this. None also means
        # "not yet resolved", so a separate flag separates the two — otherwise a home
        # chat with no owner in the very first pass compares equal to the initial
        # None and reports nothing.
        self.operator_key: Optional[str] = None
        self._operator_resolved = False
        self._http_session = None
        self._ws_tasks: dict[str, asyncio.Task] = {}
        self._reconcile_task: Optional[asyncio.Task] = None
        self._seen_message_uids: set[str] = set()
        self._stop_event = asyncio.Event()
        # The 60s poll and the tool's post-send pass both reconcile. Without this
        # they interleave, and an older /v1/chats snapshot applying second evicts a
        # thread the newer one just adopted — while the tool has already reported
        # "adopted", so replies are silently missed until the next poll.
        self._reconcile_lock = asyncio.Lock()
        self._welcome_sent = False
        # Half of the shared-group-session setting, and the smaller half: this one
        # is the adapter's in-flight dispatch guard. The key the session is
        # *persisted* under comes from gateway config
        # (`group_sessions_per_user: false`). Both or neither — one alone is a bug
        # either way, because the delivery still succeeds while the turn lands in a
        # session nobody reads.
        if not writable:
            config.extra = extra
        extra["group_sessions_per_user"] = False

    @property
    def name(self) -> str:
        return "Plow Chat"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """`is_reconnect` is accepted because the gateway passes it.

        Verified against a live Hermes: the reconnection watcher calls
        connect(is_reconnect=True), and a signature without it fails the platform
        at startup with "unexpected keyword argument" and never recovers. Keyword-
        only with a default, so callers that omit it are unaffected.
        """
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
        # A reconnect re-enters here with the previous attempt's session and
        # listeners still live. Overwriting them would orphan the group listeners,
        # which keep consuming frames while chat_uids still claims their chats — so
        # adopt_chat declines to recreate them and the subscription can never come
        # back. Tear down first; on a first connect this is a no-op.
        await self._teardown()
        # Reach and authority are dropped, not carried across. Keeping them would
        # start the retained group sockets before the poll runs, so a frame
        # arriving in that window is served with authority the *former* operator
        # earned — and the operator may have changed while the adapter was
        # offline. The poll below re-earns both before any group socket exists.
        await self._reset_poll_state()
        # Published after the teardown, so a concurrent tool call cannot adopt a
        # chat into an adapter that is mid-rebuild.
        global _live
        _live = (self, asyncio.get_running_loop())
        self._http_session = aiohttp.ClientSession()
        self._stop_event.clear()
        # One socket per chat: a ws ticket is scoped to a single chat, so reach is
        # the number of sockets. Reach is {home} at this point on every path — the
        # poll below is what adds the rest, and it adds their sockets with them.
        self._ws_tasks = {
            uid: asyncio.create_task(self._websocket_loop(uid)) for uid in self.chat_uids
        }
        # Once, before returning: reach and authority both come from this call, so
        # a delivery aimed at a group in the first seconds after a restart would
        # otherwise fail as an unknown destination.
        try:
            await self._reconcile_once()
        except Exception as exc:
            logger.warning("[plow_chat] initial reconcile failed: %s", exc)
        self._reconcile_task = asyncio.create_task(self._reconcile())
        return True

    async def _teardown(self) -> None:
        """Stop every listener, unpublish the adapter, and release the session.

        `_live` is cleared HERE rather than in disconnect(), because reconnect
        tears down too: leaving the adapter published across a teardown lets a
        concurrent tool call adopt a thread onto listeners that are being
        cancelled, and report `adopted` for a subscription that will not exist.

        Shared with the reconnect path, which has to do exactly this before it
        rebuilds — otherwise it orphans the old listeners while `chat_uids` still
        claims their chats, and `adopt_chat` then declines to recreate them.
        """
        global _live
        _live = None
        self._stop_event.set()
        tasks = [*self._ws_tasks.values(), *filter(None, [self._reconcile_task])]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._ws_tasks = {}
        self._reconcile_task = None
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def disconnect(self) -> None:
        await self._teardown()
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
        if target_chat not in self.chat_uids:
            return SendResult(success=False, error="Unknown Plow Chat destination")

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
                    f"{self.base_url}/v1/chats/{target_chat}/messages",
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

    async def start_group_thread(self, thread_handle: str, body: str) -> dict:
        """POST a new Plow/Linq thread, then reconcile so we listen to it.

        On the adapter, and on its loop, so it uses the same base URL, token and
        session as every other call — a standalone path here would send this one
        request to production while a staging install's every other call went to
        `extra.base_url`, with a live token.

        Reconciles rather than adopting the returned id directly: the poll applies
        the home-line filter, so a response naming a sibling agent's thread cannot
        make this gateway listen there.
        """
        import aiohttp

        # Unversioned on purpose. Every *documented* Plow endpoint is under /v1 and
        # the docs describe no thread-creation call at all — but this one exists
        # and routes: a live call reached it and came back 422 with a semantic
        # complaint about the phone number, which a wrong path answers 404. Do not
        # "correct" it to /v1 without a 2xx from the versioned path.
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{self.base_url}/channels/linq/send",
                json={"thread_handle": thread_handle, "text": body},
                headers={"Authorization": f"Bearer {self.token}"},
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
            await self._reconcile_once()
        except Exception as exc:
            data["adoption"] = f"failed: {type(exc).__name__}: {exc}"
            return data
        data["adoption"] = "adopted" if chat_id in self.chat_uids else "not-on-this-agents-line"
        return data

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # Plow Chat currently exposes no typing endpoint.
        return None

    def _label(self, chat_id: str) -> tuple[str, str]:
        """(display name, chat type) — the one answer both callers get."""
        group = self.groups.get(chat_id)
        if group:
            return group["name"], "group"
        return ("Plow Chat", "dm") if chat_id == self.chat_uid else (chat_id, "group")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        name, chat_type = self._label(chat_id)
        return {"name": name, "type": chat_type}

    async def _page(self, path: str) -> list[dict]:
        return (await self._page_full(path))["data"]

    async def _page_full(self, path: str) -> dict:
        """One page of a Plow list, and a complaint if there is another.

        Every list endpoint reports has_more and documents no way to ask for the
        next page. Everything read here degrades silently past that boundary while
        the poll still looks healthy — a chat never discovered, or a vouch that
        scrolled off, taking its room back behind the pairing gate. Say it rather
        than invent a cursor parameter the API does not have.
        """
        async with self._http_session.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        ) as resp:
            page = await _body(resp)
        if page.get("has_more"):
            logger.warning("[plow_chat] %s has a page we cannot reach; the rest is unseen", path)
        return page

    async def _reset_poll_state(self) -> None:
        """Discard everything the poll established, back to a bare home chat.

        Two callers reach this state — a reconnect, and a complete listing with no
        home chat — and they were writing the same four fields separately. That is
        the partial-state seam every lifecycle defect in this file has come from,
        so it gets one owner like reach did.
        """
        self.operator_key = None
        self._operator_resolved = False
        self.operator_vouched.clear()
        await self._set_reach({self.chat_uid})

    async def _set_reach(self, wanted: "frozenset[str] | set[str]") -> None:
        """Make reach, sockets and vouches agree with `wanted`, in both directions.

        The single owner of per-chat lifecycle. Every defect this layer has had was
        a transition that updated some of {chat_uids, _ws_tasks, operator_vouched}
        and not the rest — a group claimed with no socket, a socket left running
        with no claim, a vouch outliving the room it was made in. There is one way
        to change reach now, and it changes all three together.
        """
        wanted = frozenset(wanted) | {self.chat_uid}
        for gone in self.chat_uids - wanted:
            task = self._ws_tasks.pop(gone, None)
            if task and not task.done():
                task.cancel()
            # The vouch dies with the room. It was earned in a chat this agent can
            # no longer see on its line, so it may not survive to authorize a
            # chat that reappears under the same id later.
            self.operator_vouched.discard(gone)
            logger.info("[plow_chat] left %s; no longer on this agent's line", gone)
        for added in wanted - self.chat_uids:
            self._ws_tasks[added] = asyncio.create_task(self._websocket_loop(added))
            group = self.groups.get(added)
            if group:
                # Distinguished, because the two say opposite things to an operator
                # reading the log: this one is the configured group they expect, and
                # telling them to configure it would send them chasing a no-op.
                logger.info("[plow_chat] joined configured group %s (%s), members authorized",
                            added, group["name"])
            else:
                logger.info(
                    "[plow_chat] adopted %s; add '%s=<display name>' to "
                    "PLOW_CHAT_GROUP_UIDS to name it and authorize its members",
                    added, added)
        self.chat_uids = wanted

    async def adopt_chat(self, chat_uid: str) -> bool:
        """Add one chat to reach. Reach only — adoption never authorizes anyone.

        The model chooses the recipients of a thread it starts, so an injected
        instruction must not be able to hand out this runtime's tools that way. The
        room answers under GROUP_POLICY like any other, and its members stay behind
        the pairing gate until the operator speaks there.

        False means it was already subscribed, so a retried send cannot leave two
        websockets on one chat each handing the agent the same message.
        """
        if chat_uid in self.chat_uids:
            return False
        await self._set_reach(self.chat_uids | {chat_uid})
        return True

    async def _reconcile_once(self) -> None:
        async with self._reconcile_lock:
            # Classified once, before anything indexes it. Reading chat["uid"] across
            # the whole listing to find home would abort the sweep on one entry
            # missing the field — the very failure this loop is hardened against.
            listing = await self._page_full("/v1/chats")
            chats = []
            for chat in listing["data"]:
                if isinstance(chat, dict) and chat.get("uid"):
                    chats.append(chat)
                else:
                    logger.warning("[plow_chat] skipping listing entry with no uid")
            home = [chat for chat in chats if chat["uid"] == self.chat_uid]
            if not home:
                # The home chat anchors both halves of this: which line is ours, and who
                # the operator is. Without it every chat on the account looks like one
                # of ours, so we never *widen* reach here.
                if listing.get("has_more"):
                    # It may simply be on a page we could not fetch. Carry on unchanged
                    # rather than tear down a working agent over an incomplete view.
                    logger.warning("[plow_chat] home chat %s not on this page; listing is "
                                   "truncated, so leaving reach as it is", self.chat_uid)
                    return
                # A complete listing without it means the anchor is genuinely gone.
                # Returning early used to leave every earned vouch and socket standing,
                # so members kept gateway-wide pairing and tool authority with nothing
                # establishing the line they were granted on.
                logger.error("[plow_chat] home chat %s is absent from a complete listing; "
                             "dropping all group reach and authority", self.chat_uid)
                await self._reset_poll_state()
                return
            # GET /v1/chats is user-wide — the token is a user credential — so the home
            # chat's line is what says which of these threads are ours to answer in.
            home_line = _agent_line(home[0])
            # One odd chat must cost one chat, not the sweep. _agent_line returns None
            # for anything it cannot classify; letting that raise inside the
            # comprehension would abort discovery for the life of the process, and the
            # symptom — one thread silently never answering — reads like anything else.
            mine = []
            for chat in chats:
                line = _agent_line(chat)
                if line is None:
                    logger.warning("[plow_chat] skipping unclassifiable chat %s",
                                   chat.get("uid", "<no uid>"))
                elif line == home_line:
                    mine.append(chat)
            home_members = _members(home[0])
            # Every owner-role participant IS the operator, so which one is arbitrary —
            # but the pick has to be stable, or a reordered listing reads as an identity
            # change and revokes every vouch on each poll.
            owner_keys = [k for m in home_members if _is_owner(m) and (k := m.get("provider_key"))]
            next_operator = min(owner_keys, default=None)
            if next_operator != self.operator_key or not self._operator_resolved:
                self._operator_resolved = True
                # Grants do not outlive the identity that made them. `authorized` only
                # ever grows, so clearing the key alone left rooms vouched by an
                # identity that no longer holds — members kept tool authority and
                # pairing approval from an operator that is gone. Configured groups
                # keep theirs: that authority comes from the dotenv plus the line
                # check, never from the operator.
                revoked = set(self.operator_vouched)
                self.operator_vouched.clear()
                if revoked:
                    logger.warning("[plow_chat] operator identity changed; revoked "
                                   "operator-vouched authority for %s", sorted(revoked))
                self.operator_key = next_operator
                if next_operator is None:
                    logger.error("[plow_chat] no owner among the home chat's %d member "
                                 "participant(s) — nobody there holds the account, or the API "
                                 "is not serving `role` at all, in which case no room can be "
                                 "vouched either. An operator change can no longer be seen, so "
                                 "vouched authority will not be revoked",
                                 len(home_members))
            # Reach is settled FIRST, before any vouch hydration. The hydration below
            # does network reads that can fail, and a failure there used to abort the
            # pass before removal ever ran — so a departed room kept its socket and its
            # authority because reading some *other* room's history timed out.
            wanted = {chat["uid"] for chat in mine}
            if listing.get("has_more"):
                # Only the ids this page could not speak to. A chat that IS on this page
                # and not on our line has been positively classified as a sibling's —
                # unioning it back would keep dispatching a room we can see is not ours.
                # Absent from the page is the only case where "gone" is unproven.
                visible = {chat["uid"] for chat in chats}
                wanted |= self.chat_uids - visible
            await self._set_reach(wanted)

            # Now hydrate authority for what survived. A revoked room is no longer in
            # operator_vouched, so this re-reads its history against the new identity
            # and re-earns the vouch in the same pass when the new operator has in fact
            # spoken there.
            for chat_uid in sorted(self.chat_uids):
                # A configured group needs no vouch: being in chat_uids means the line
                # check passed, and that plus self.groups *is* its authority.
                if (chat_uid == self.chat_uid or chat_uid in self.groups
                        or chat_uid in self.operator_vouched):
                    continue
                # Decided every pass until earned. A vouch that landed while the socket
                # was down reached no frame handler, so a room the operator spoke in
                # would otherwise stay behind the gate for the life of the process.
                if any(_is_owner(msg.get("sender") or {})
                       for msg in await self._page(f"/v1/chats/{chat_uid}/messages")
                       if isinstance(msg, dict) and msg.get("direction") == "inbound"):
                    self.operator_vouched.add(chat_uid)
            # A configured id we never saw on our line is a stale or mistyped dotenv
            # entry naming a sibling agent's chat. Say so rather than silently never
            # joining a group the operator believes is configured.
            stranded = sorted(set(self.groups) - {chat["uid"] for chat in mine})
            if stranded:
                logger.warning("[plow_chat] configured group(s) not on this agent's line, "
                               "so not joined: %s", stranded)

    async def _reconcile(self) -> None:
        """Adopt the chats nobody configured — polled, because nothing pushes them.

        A thread the operator starts from Messages is on the account the moment it
        lands, but no frame ever says so: a ws ticket is per-chat and there is no
        webhook, so listing is the documented way to learn of a new chat. Without
        this, the dotenv is the only way a chat becomes audible.
        """
        while not self._stop_event.is_set():
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Loud but not fatal. A poll that dies takes discovery with it for
                # the life of the process, and the symptom — one thread silently
                # never answering — reads like anything but this.
                logger.warning("[plow_chat] chat reconcile failed: %s", exc)
            await asyncio.sleep(RECONCILE_SECONDS)

    async def _mint_ws_ticket(self, chat_uid: str) -> str:
        async with self._http_session.post(
            f"{self.base_url}/v1/ws/ticket",
            json={"chat_id": chat_uid},
            headers={"Authorization": f"Bearer {self.token}"},
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                err = data.get("error", {}) if isinstance(data, dict) else {}
                raise RuntimeError(err.get("message") or f"ticket mint failed: {resp.status}")
            return data["ticket"]

    async def _websocket_loop(self, chat_uid: str) -> None:
        import aiohttp

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                ticket = await self._mint_ws_ticket(chat_uid)
                ws_url = _ws_url_for(self.base_url, ticket)
                async with self._http_session.ws_connect(ws_url, heartbeat=30) as ws:
                    backoff = 1.0
                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_frame(chat_uid, msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[plow_chat] websocket loop error (%s): %s",
                    chat_uid,
                    _redact_ticket("%s" % exc),
                )
                # Adapter-wide health follows the home chat only. With N sockets a
                # shared flag tracks the worst chat, not reachability: one group
                # deleted server-side would flap the adapter disconnected every
                # backoff cycle while the agent stayed perfectly reachable.
                if chat_uid == self.chat_uid and self.is_connected:
                    self._mark_disconnected()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle_ws_frame(self, chat_uid: str, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "connected":
            if chat_uid == self.chat_uid:
                self._mark_connected()
            logger.info("[plow_chat] websocket subscribed (%s)", chat_uid)
            return
        if frame_type == "chat_active":
            logger.info("[plow_chat] chat active")
            await self._send_activation_welcome()
            return
        if frame_type == "participant_verified":
            if self._may_approve(chat_uid):
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
        # A ticket is per-chat, but a frame still names its chat. A mismatch is a
        # shape this code does not know, and attributing it to the socket's chat
        # would file one room's message under another.
        if (message.get("chat_uid") or chat_uid) != chat_uid:
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

        # The vouch is recorded first, because this very frame can be the thing
        # that earns it. Asking _may_approve() before it meant the operator's own
        # first message in an adopted room missed pairing and hit the generic gate,
        # while the same frame went on to be dispatched with role_authorized=True.
        #
        # The operator speaking is the one signal the model cannot manufacture: it
        # cannot send a message *as* the operator. Presence would not do — an
        # injected instruction can put the operator in a room with an attacker.
        # Speaking there is a choice.
        if _is_owner(sender):
            self.operator_vouched.add(chat_uid)

        # One decision, used for both. Pairing is keyed by (platform, user id), not
        # by chat, so approving makes that person a paired user of the whole
        # gateway — it has to follow tool authority exactly, and computing it twice
        # is how the two drift.
        authorized = self._may_approve(chat_uid)
        if authorized:
            self._approve_plow_member(user_id, user_name)

        chat_name, chat_type = self._label(chat_uid)
        source_kwargs: dict[str, Any] = dict(
            chat_id=chat_uid,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )
        event_kwargs: dict[str, Any] = dict(
            text=text,
            message_type=MessageType.TEXT,
            raw_message=frame,
            message_id=msg_uid,
        )
        # Vouched-for rooms only: named in the dotenv, or vouched for by the
        # operator speaking there. A chat the *model* started is in neither, so an
        # injected instruction cannot hand out this runtime's tools.
        source_kwargs["role_authorized"] = authorized
        if chat_type != "dm":
            event_kwargs["channel_prompt"] = _channel_prompt(self.groups.get(chat_uid), sender)
        await self.handle_message(MessageEvent(source=self.build_source(**source_kwargs), **event_kwargs))

    def _may_approve(self, chat_uid: str) -> bool:
        """Whether this chat's members may use tools and be paired.

        One answer for both, because PairingStore is keyed by (platform, user id):
        an approval granted in one room is an approval everywhere, so pairing has
        to follow tool authority exactly.

        Configured authority is derived here rather than stored — a named group
        counts only once the poll has put it in chat_uids, which is the line check.
        """
        if chat_uid == self.chat_uid or chat_uid in self.operator_vouched:
            return True
        return chat_uid in self.groups and chat_uid in self.chat_uids

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


def _flag(value: Any, *, default: bool, safe: bool) -> bool:
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


def _normalize_thread_handle(recipients: list[str] | None) -> str:
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


def _plow_start_group_message(args: dict, **_kwargs) -> str:
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
                "recipient_count": len([r for r in recipients if str(r).strip()]),
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
    # Registered unconditionally, like the platform itself: group chats are handled
    # by default, so gating the tool that starts one on a config nobody has to set
    # would leave it permanently unreachable on a stock install.
    ctx.register_tool(
        name="plow_start_group_message",
        toolset="plow_chat",
        schema=PLOW_START_GROUP_MESSAGE_SCHEMA,
        handler=_plow_start_group_message,
        check_fn=lambda: bool(os.getenv("PLOW_CHAT_TOKEN")),
        requires_env=["PLOW_CHAT_TOKEN"],
    )
