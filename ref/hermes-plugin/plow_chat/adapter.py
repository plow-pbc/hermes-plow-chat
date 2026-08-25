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

GROUP_POLICY = (
    "This is a shared group chat. Respond only when you reasonably infer, "
    "as a human participant would, that Hermes is directly or contextually "
    "addressed or that a response is clearly expected. Otherwise output "
    f"exactly {SILENT_REPLY_TOKEN} and no other text."
)

# How long a thread can sit unheard after the operator adds Hermes to it. One
# call a minute against a list that changes a few times a year; the cost of
# shortening it is quota, of lengthening it somebody repeating themselves.
RECONCILE_SECONDS = 60


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


def _channel_prompt(group: dict | None) -> str:
    """GROUP_POLICY always leads; a configured group's prompt appends, never replaces.

    An adopted chat has no configured prompt, so it gets the bare policy — it is a
    room Hermes was added to, not one the operator described.
    """
    if group is None or not group["prompt"]:
        return GROUP_POLICY
    return f"{GROUP_POLICY}\n\n{group['prompt']}"


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
        # The handle that owns this agent, learned off the home chat by the poll.
        # None also means "ambiguous", so a separate flag marks whether the poll has
        # ever resolved it — otherwise a home chat that is ambiguous from the very
        # first pass compares equal to the initial None and reports nothing.
        self.operator_key: Optional[str] = None
        self._operator_resolved = False
        self._http_session = None
        self._ws_tasks: dict[str, asyncio.Task] = {}
        self._reconcile_task: Optional[asyncio.Task] = None
        self._seen_message_uids: set[str] = set()
        self._stop_event = asyncio.Event()
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
        self._http_session = aiohttp.ClientSession()
        self._stop_event.clear()
        # One socket per chat: a ws ticket is scoped to a single chat, so reach is
        # the number of sockets. On a reconnect chat_uids already carries the
        # groups the poll validated onto this line, and they need sockets too.
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
        """Stop every listener and release the session.

        Shared with the reconnect path, which has to do exactly this before it
        rebuilds — otherwise it orphans the old listeners while `chat_uids` still
        claims their chats, and `adopt_chat` then declines to recreate them.
        """
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
        # Positional, so pin the shape rather than guess. operator_key is the sole
        # credential that grants tool authority to a room; picking arbitrarily out
        # of a multi-member home chat is a silent authority escalation.
        home_members = _members(home[0])
        # None when the home chat is ambiguous: choosing positionally out of several
        # members would let an arbitrary participant vouch rooms.
        next_operator = home_members[0]["provider_key"] if len(home_members) == 1 else None
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
                logger.error("[plow_chat] home chat has %d member participants, expected 1; "
                             "operator cleared, so operator-derived authorization is off",
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
            if any(self._is_operator(msg.get("sender") or {})
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
                logger.warning("[plow_chat] websocket loop error (%s): %s", chat_uid, exc)
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
        if self._is_operator(sender):
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
            event_kwargs["channel_prompt"] = _channel_prompt(self.groups.get(chat_uid))
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

    def _is_operator(self, sender: dict) -> bool:
        """Whether a message came from the operator, by provider_key.

        provider_key is the only handle that identifies a person across chats: a
        chat_participant uid is per-chat, and the roster is not authoritative
        either — it grows as quiet members first speak. The message carries the
        answer, so the message is what is asked.

        Not a bare equality: an agent-sent frame carries no provider_key, and
        before the first poll there is no key to compare against, so None == None
        would make the gateway's own traffic the operator's.
        """
        return bool(self.operator_key) and sender.get("provider_key") == self.operator_key

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
