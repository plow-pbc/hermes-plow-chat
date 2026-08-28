"""Unit coverage for the plow_chat adapter's startup baseline and multi-chat behavior.

The adapter runs inside hermes, so `gateway.*` is stubbed below and the module
is loaded straight from `plow-chat-platform/__init__.py` — these tests exercise
the adapter without adding Hermes itself as a dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import pathlib
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "plow-chat-platform" / "__init__.py"


@dataclass
class _SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Any:
    """Import the plugin against stub `gateway` modules."""
    config = types.ModuleType("gateway.config")
    config.HomeChannel = lambda **kw: kw  # type: ignore[attr-defined]
    config.Platform = lambda name: name  # type: ignore[attr-defined]
    config.persist_home_channel = lambda *a, **k: None  # type: ignore[attr-defined]

    base = types.ModuleType("gateway.platforms.base")

    class _AttrDict(dict[str, Any]):
        __getattr__ = dict.__getitem__

    class _Adapter:
        def __init__(self, *, config: Any, platform: Any) -> None:
            self.config = config

        def build_source(self, **kw: Any) -> Any:
            return _AttrDict(kw)

        async def handle_message(self, event: Any) -> None: ...

        def _mark_connected(self) -> None: ...
        def _mark_disconnected(self) -> None: ...

    base.BasePlatformAdapter = _Adapter  # type: ignore[attr-defined]
    base.MessageEvent = lambda **kw: _AttrDict(kw)  # type: ignore[attr-defined]
    base.SendResult = _SendResult  # type: ignore[attr-defined]

    for name, module in {
        "gateway": types.ModuleType("gateway"),
        "gateway.config": config,
        "gateway.platforms": types.ModuleType("gateway.platforms"),
        "gateway.platforms.base": base,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    # The checkpoint base honors the fleet's HERMES_HOME; pin it here so the
    # module-scope default never points a test at /var/lib/hermes.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PLOW_HOME_CHANNEL", "cht_a")
    monkeypatch.setenv("PLOW_AGENT_TOKEN", "plow_tok")  # pragma: allowlist secret — a fixture string
    spec = importlib.util.spec_from_file_location("plow_chat_under_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # No CHECKPOINT override: with HERMES_HOME pinned above, the module's own
    # env-derived resolution already lands in tmp_path -- the assert IS the
    # regression pin for the fleet's checkpoint home (the old hardcoded
    # /var/lib/hermes made every fleet anchor raise).
    assert module.CHECKPOINT == tmp_path / "plow_chat_last_uid"
    return module


class _WS:
    """A socket that connects and delivers nothing, so `_listen` runs exactly one
    iteration: backfill, an empty frame loop, then round to the retry sleep."""

    async def __aenter__(self) -> "_WS":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...

    def __aiter__(self) -> "_WS":
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class _Session:
    """One socket protocol for every case: an anchor read, a backfill page, a
    ticket, and a connection. `calls` records the order, which is the property
    most of these tests are actually about."""

    def __init__(self, *, anchor=(), backfill=(), status=200, calls=None):
        self.anchor, self.backfill, self.status = list(anchor), list(backfill), status
        self.calls = calls if calls is not None else []

    def get(self, url: str, **kw: Any) -> "_Resp":
        anchoring = "limit=1" in url
        self.calls.append("history" if anchoring else "backfill")
        if self.status >= 400:
            return _Resp({}, status=self.status)
        return _Resp({"data": self.anchor if anchoring else self.backfill, "has_more": False})

    def post(self, url: str, **kw: Any) -> "_Resp":
        self.calls.append("ticket")
        if getattr(self, "ticket_status", 200) != 200:
            return _Resp({}, status=self.ticket_status)
        return _Resp({"ticket": "tkt"})

    def ws_connect(self, url: str, **kw: Any) -> "_WS":
        self.calls.append("ws_connect")
        return _WS()

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...
    async def json(self, content_type: Any = None) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


def _chat(uid: str, *, name: str | None = None, group: bool = False) -> dict[str, Any]:
    participants = [
        {"type": "agent"},
        {"type": "member", "uid": f"mem_owner_{uid}", "role": "owner"},
    ]
    if group:
        participants.append({"type": "member", "uid": f"mem_other_{uid}", "role": "member"})
    return {"uid": uid, "display_name": name, "participants": participants}


def _envelope(event_id: str, chat_id: str, message_id: str, *, role: str = "owner") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "message_received",
        "chat_id": chat_id,
        "data": {
            "type": "message_received",
            "message": {
                "uid": message_id,
                "body": message_id,
                "direction": "inbound",
                "sender": {
                    "type": "member",
                    "uid": f"mem_{role}_{chat_id}",
                    "display_name": role.title(),
                    "role": role,
                },
            },
        },
    }


def test_member_turn_hook_is_registered_and_blocks_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    module = _load(monkeypatch, tmp_path)
    hooks: dict[str, Any] = {}

    class _Context:
        def register_hook(self, name: str, callback: Any) -> None:
            hooks[name] = callback

        def register_platform(self, **kwargs: Any) -> None: ...
        def register_tool(self, **kwargs: Any) -> None: ...

    module.register(_Context())
    hook = hooks["pre_tool_call"]

    assert hook() is None
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    turn = adapter._member_turn_chat.set("cht_b")
    try:
        assert hook(
            tool_name="terminal",
            args={},
            task_id="task",
            session_id="session",
            tool_call_id="call",
            turn_id="turn",
            api_request_id="request",
            middleware_trace=[],
        ) == {"action": "block", "message": "tools are unavailable on this turn"}
    finally:
        adapter._member_turn_chat.reset(turn)


async def test_busy_queued_member_turn_uses_its_own_tool_gate_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a", group=True)])

    owner_started = asyncio.Event()
    release_owner = asyncio.Event()
    turns_done = asyncio.Event()
    observed: list[tuple[str, Any]] = []
    typing_tasks: dict[str, Any] = {}
    pending: Any = None
    active = False

    async def run_turn(event: Any) -> None:
        nonlocal active, pending
        on_start = getattr(adapter, "on_processing_start", None)
        if on_start:
            await on_start(event)
        typing_tasks[event.message_id] = adapter._typing[event.source.chat_id]
        try:
            observed.append((event.message_id, module._block_member_tools()))
            if event.message_id == "msg_owner":
                owner_started.set()
                await release_owner.wait()
        finally:
            on_complete = getattr(adapter, "on_processing_complete", None)
            if on_complete:
                await on_complete(event, None)

        if pending is not None:
            next_event, pending = pending, None
            asyncio.create_task(run_turn(next_event))
        else:
            active = False
            turns_done.set()

    async def enqueue(event: Any) -> None:
        nonlocal active, pending
        if active:
            pending = event
            return
        active = True
        asyncio.create_task(run_turn(event))

    monkeypatch.setattr(adapter, "handle_message", enqueue)
    await adapter._on_frame(_envelope("evt_owner", "cht_a", "msg_owner"))
    await asyncio.wait_for(owner_started.wait(), timeout=1)
    await adapter._on_frame(_envelope("evt_member", "cht_a", "msg_member", role="member"))
    queued_kept_owner_typing = adapter._typing["cht_a"] is typing_tasks["msg_owner"]
    release_owner.set()
    await asyncio.wait_for(turns_done.wait(), timeout=1)

    assert observed == [
        ("msg_owner", None),
        ("msg_member", {"action": "block", "message": "tools are unavailable on this turn"}),
    ]
    assert queued_kept_owner_typing
    assert typing_tasks["msg_owner"] is not typing_tasks["msg_member"]
    assert not adapter._typing


@pytest.mark.parametrize(
    "history,history_status,connects,baseline",
    [
        ([{"uid": "msg_before_connect"}], 200, True, "msg_before_connect"),
        ([], 200, True, None),
        (None, 503, False, None),
    ],
    ids=[
        "chat has history -> anchored before the socket",
        "chat is empty -> no baseline, and the backfill still pages",
        "history unreachable -> no socket, no baseline",
    ],
)
async def test_startup_baseline_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    history: list[dict[str, str]] | None,
    history_status: int,
    connects: bool,
    baseline: str | None,
) -> None:
    """Where the starting baseline comes from, and what happens when it cannot.

    Ordering is the property under test, because it is the one that fails
    silently. Anchoring after `ws_connect` races the frames that connection is
    already buffering: a message committed after connect gets swept into the
    baseline, loses its frame before iteration reaches it, and the next
    reconnect pages back only to a uid that was never handled — dropping the
    customer's very first turn.

    An empty chat is the case that bit twice. It legitimately leaves the
    baseline unset, and an early return on "no baseline" meant a brand-new
    chat's first message was lost if the socket dropped before hermes accepted
    it. With nothing to stop at, the backfill pages to exhaustion instead.

    An unreachable history must not connect at all: an agent with no
    recoverable baseline is exactly the state the checkpoint rules out, and
    `_listen` already owns retrying a broken API.
    """
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))

    calls: list[str] = []
    session = _Session(anchor=history or [], status=history_status, calls=calls)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: session)
    with mock.patch.object(module.asyncio, "sleep", side_effect=StopAsyncIteration):
        with pytest.raises(StopAsyncIteration):
            await adapter._listen()

    assert ("ws_connect" in calls) is connects, calls
    if connects:
        assert calls.index("history") < calls.index("ws_connect"), "the baseline must predate anything the socket carries"
    if connects:
        # Even with no baseline the backfill must run: that is the empty-chat
        # first turn, which an early return on "no baseline" used to drop.
        assert "backfill" in calls, calls

    assert adapter._last_uids[adapter.home_chat_uid] == baseline
    checkpoint = tmp_path / "plow_chat_last_uid"
    if connects:
        # The file's existence is what records "this agent has anchored" — its
        # contents are the cursor, empty when the chat was empty. A restart has
        # to be able to tell those apart from never having anchored at all.
        assert checkpoint.exists()
        assert checkpoint.read_text() == (baseline or "")
    else:
        assert not checkpoint.exists(), "an agent that never connected must not look anchored"


async def test_a_restart_does_not_re_anchor_over_messages_it_never_handled(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Anchoring is remembered on disk, because the process does not survive.

    `hermes-gateway.service` is `Restart=always`. A process-local "already
    anchored" flag reset on every restart, so an agent that first anchored an
    empty chat would anchor again on the way back up — sweeping a turn sent
    during the restart into the baseline as pre-existing and never handing it
    to hermes. The checkpoint file is the one durable owner of that state.
    """
    module = _load(monkeypatch, tmp_path)
    checkpoint = tmp_path / "plow_chat_last_uid"
    checkpoint.write_text("")  # anchored earlier, on an empty chat

    calls: list[str] = []
    session = _Session(anchor=[{"uid": "should_not_be_read"}], backfill=[{"uid": "msg_during_restart"}], calls=calls)

    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    assert adapter._anchored_chats[adapter.home_chat_uid], "an existing checkpoint means this agent already anchored"

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: session)
    handled: list[str] = []
    monkeypatch.setattr(adapter, "_on_message", lambda m, _chat_uid: handled.append(m["uid"]))
    with mock.patch.object(module.asyncio, "sleep", side_effect=StopAsyncIteration):
        with pytest.raises(StopAsyncIteration):
            await adapter._listen()

    assert "history" not in calls, "re-anchoring would swallow the turn sent during the restart"
    assert handled == ["msg_during_restart"], "it must be backfilled instead"


async def test_an_initial_marker_that_will_not_persist_does_not_connect(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """In-memory anchor state follows the disk; it never leads it.

    Setting it before the atomic write landed left this process believing it had
    anchored while the next one, reading the file, disagreed — and that restart
    re-anchored, sweeping whatever arrived in between into the baseline. An
    unwritable checkpoint is therefore a connection failure, not a warning.
    """
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))

    calls: list[str] = []
    session = _Session(anchor=[{"uid": "msg_a"}], calls=calls)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: session)
    monkeypatch.setattr(module.os, "replace", mock.Mock(side_effect=OSError("read-only volume")))

    with mock.patch.object(module.asyncio, "sleep", side_effect=StopAsyncIteration):
        with pytest.raises(StopAsyncIteration):
            await adapter._listen()

    assert "ws_connect" not in calls, "connecting here would serve turns it could never recover"
    assert not adapter._anchored_chats[adapter.home_chat_uid]
    assert adapter._last_uids[adapter.home_chat_uid] is None, "state must not claim what the disk does not hold"


async def test_one_socket_demuxes_and_checkpoints_two_chats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _load(monkeypatch, tmp_path)
    config = SimpleNamespace(extra={})
    adapter = module.PlowChatAdapter(config)
    adapter._set_reach([_chat("cht_a"), _chat("cht_b", name="Project room", group=True)])

    handled: list[dict[str, Any]] = []

    async def capture(event: dict[str, Any]) -> None:
        handled.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)
    with caplog.at_level(logging.WARNING):
        await adapter._on_frame(_envelope("evt_a", "cht_a", "msg_a"))
        await adapter._on_frame(_envelope("evt_b", "cht_b", "msg_b_owner"))
        await adapter._on_frame(_envelope("evt_b", "cht_b", "msg_duplicate"))
        await adapter._on_frame(_envelope("evt_out", "cht_c", "msg_out"))
        await adapter._on_frame(_envelope("evt_b_member", "cht_b", "msg_b_member", role="member"))

    assert [(event["source"]["chat_id"], event["message_id"]) for event in handled] == [
        ("cht_a", "msg_a"),
        ("cht_b", "msg_b_owner"),
        ("cht_b", "msg_b_member"),
    ]
    owner_source, member_source = handled[1]["source"], handled[2]["source"]
    assert (owner_source["chat_name"], owner_source["chat_type"]) == ("Project room", "group")
    assert (owner_source["chat_id"], owner_source["chat_type"]) == (
        member_source["chat_id"],
        member_source["chat_type"],
    )
    assert owner_source["role_authorized"] is True
    assert member_source["role_authorized"] is False
    # Prompt CONTENT is pinned by block identity, not substrings: a substring
    # scan survives a rewrite that inverts the meaning. This is a GROUP, so the
    # owner turn carries the shared-thread rules too — the room is the
    # boundary, not the asker.
    owner_prompt = handled[1]["channel_prompt"]
    assert owner_prompt == module.GROUP_OWNER_CHANNEL_PROMPT
    for block in (module._DISCLOSURE, module._NO_RELAY):
        assert block in owner_prompt
    member_prompt = handled[2]["channel_prompt"]
    assert member_prompt == module.EXTERNAL_CHANNEL_PROMPT
    for block in (module._SPEAKER_FACT, module._DISCLOSURE, module._NO_RELAY):
        assert block in member_prompt
    assert module._SPEAKER_FACT not in owner_prompt, "the owner is not a member"
    assert "first-user onboarding" not in owner_prompt.lower()
    assert config.extra["group_sessions_per_user"] is False
    assert (tmp_path / "plow_chat_last_uid").read_text() == "msg_a"
    assert (tmp_path / "plow_chat_last_uid.cht_b").read_text() == "msg_b_member"
    assert "outside the grant" in caplog.text


class _HTTP:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _HTTP:
        return self

    async def __aexit__(self, *exc: Any) -> None: ...

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
        self.posts.append((url, json))
        return _Resp({"uid": "msg_sent"})


async def test_a_grant_that_drops_the_configured_home_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The home is where cron and the owner's default output land. When the
    grant no longer contains it, the old fallback adopted whichever chat the
    API listed first -- pointing owner-directed deliveries at an unrelated
    room. The contract now is refusal: reach stays as it was, nothing is
    persisted, and _listen retries with an error naming the fix."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a"), _chat("cht_b")])
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "persist_home_channel", lambda home, **kwargs: persisted.append(home))

    class _GrantHTTP:
        def get(self, url: str, **kwargs: Any) -> _Resp:
            return _Resp({"object": "list", "data": [_chat("cht_b"), _chat("cht_c")], "has_more": False})

    with pytest.raises(RuntimeError, match=r"not in the.*credential grant"):
        await adapter._refresh_reach(_GrantHTTP())
    assert adapter.home_chat_uid == "cht_a", "a refused grant must not move the home"
    assert adapter.chat_uids == frozenset({"cht_a", "cht_b"}), "a refused grant must not replace reach"
    assert persisted == []


class _SocketHTTP(_HTTP):
    def __init__(self) -> None:
        super().__init__()
        self.gets: list[str] = []
        self.sockets: list[str] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
        self.posts.append((url, json))
        return _Resp({"ticket": "tkt_granted"})

    def get(self, url: str, *, headers: dict[str, str]) -> _Resp:
        self.gets.append(url)
        return _Resp({"data": [], "has_more": False})

    def ws_connect(self, url: str, *, heartbeat: int) -> _WS:
        self.sockets.append(url)
        return _WS()


async def test_two_chat_reach_opens_one_granted_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a"), _chat("cht_b")])
    http = _SocketHTTP()
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda: http)
    greetings: list[str] = []

    async def greet(chat_id: str, content: str, **kwargs: Any) -> _SendResult:
        greetings.append(chat_id)
        if chat_id == "cht_a":
            raise RuntimeError("send failed after commit")
        return _SendResult(success=True)

    monkeypatch.setattr(adapter, "send", greet)

    for _ in range(2):
        with mock.patch.object(module.asyncio, "sleep", side_effect=StopAsyncIteration):
            with pytest.raises(StopAsyncIteration):
                await adapter._listen()

    assert http.posts == [(f"{module.BASE}/v1/ws/ticket", {})] * 2
    assert len(http.sockets) == 2
    assert sorted(greetings) == ["cht_a", "cht_b"], "each chat is latched before its one greeting attempt"
    assert {url.split("/v1/chats/")[1].split("/")[0] for url in http.gets} == {"cht_a", "cht_b"}

    # A NEW process over the same checkpoints must not greet again: the wave is
    # a first-meeting disclosure, and an in-memory latch alone re-sent it to
    # every granted chat on every gateway restart.
    restarted = module.PlowChatAdapter(SimpleNamespace(extra={}))
    restarted._set_reach([_chat("cht_a"), _chat("cht_b")])
    monkeypatch.setattr(restarted, "send", greet)
    with mock.patch.object(module.asyncio, "sleep", side_effect=StopAsyncIteration):
        with pytest.raises(StopAsyncIteration):
            await restarted._listen()
    assert sorted(greetings) == ["cht_a", "cht_b"], "a restart re-greeted an already-met chat"


async def test_send_uses_the_turn_chat_and_refuses_ungranted_or_cross_chat_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a"), _chat("cht_b", group=True)])
    http = _HTTP()
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda: http)

    results: dict[str, _SendResult] = {}

    async def reply_from_member_turn(event: Any) -> None:
        await adapter.on_processing_start(event)
        try:
            if event.message_id == "msg_no_reply":
                return
            results["reply"] = await adapter.send(event.source.chat_id, "reply in B")
            results["cross_chat"] = await adapter.send("cht_a", "must not leave B")
        finally:
            await adapter.on_processing_complete(event, None)

    monkeypatch.setattr(adapter, "handle_message", reply_from_member_turn)
    await adapter._on_frame(_envelope("evt_b", "cht_b", "msg_b", role="member"))
    await adapter._on_frame(_envelope("evt_no_reply", "cht_b", "msg_no_reply", role="member"))
    results["after_turn"] = await adapter.send("cht_a", "allowed after B")
    results["outside_grant"] = await adapter.send("cht_c", "not granted")

    assert "cht_b" not in adapter._typing
    assert results["reply"].success
    assert not results["cross_chat"].success
    assert results["after_turn"].success
    assert not results["outside_grant"].success
    assert http.posts == [
        (f"{module.BASE}/v1/chats/cht_b/messages", {"body": "reply in B"}),
        (f"{module.BASE}/v1/chats/cht_a/messages", {"body": "allowed after B"}),
    ]


async def test_anchor_failure_names_the_chat_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a"), _chat("cht_b")])

    class _AnchorHTTP:
        def get(self, url: str, *, headers: dict[str, str]) -> _Resp:
            return _Resp({"data": []})

    monkeypatch.setattr(adapter, "_checkpoint", lambda uid, chat_uid: False)
    with pytest.raises(OSError) as error:
        await adapter._anchor(_AnchorHTTP(), "cht_b")

    assert str(error.value) == f"could not persist the initial baseline at {tmp_path / 'plow_chat_last_uid.cht_b'}"


# --- prompt rules and the group-send tool (ported from the operator-model adapter) ---


def test_external_turn_prompt_carries_disclosure_no_relay_and_ownership(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The three canonical group rules ride every external turn: the room-scoped
    disclosure boundary, the no-relay fact, and who owns this agent."""
    module = _load(monkeypatch, tmp_path)
    p = module.EXTERNAL_CHANNEL_PROMPT.lower()
    assert "do not reveal" in p           # disclosure
    assert "already" in p                 # no-relay: they already received it
    assert "does not own" in p            # speaker-ownership fact


def test_owner_turn_prompt_names_ownership(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    module = _load(monkeypatch, tmp_path)
    assert "owner" in module.OWNER_CHANNEL_PROMPT.lower()


class _ToolContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_hook(self, name: str, callback: Any) -> None: ...
    def register_platform(self, **kwargs: Any) -> None: ...

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)


def test_group_send_tool_registers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    module = _load(monkeypatch, tmp_path)
    ctx = _ToolContext()
    module.register(ctx)
    assert [t["name"] for t in ctx.tools] == ["plow_start_group_message"]
    tool = ctx.tools[0]
    assert tool["schema"]["name"] == "plow_start_group_message"
    assert tool["requires_env"] == ["PLOW_AGENT_TOKEN"]
    assert tool["check_fn"]()


def _live_tool(module: Any, monkeypatch: pytest.MonkeyPatch, *, result=None, raises=None, record=None):
    """Publish a live adapter whose start_group_thread is stubbed.

    The tool goes through the adapter's own seam, so there is no standalone
    POST to patch: a disconnected gateway cannot send at all.
    """
    import threading

    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))

    async def stub(thread_handle: str, body: str) -> dict[str, Any]:
        if record is not None:
            record.append((thread_handle, body))
        if raises is not None:
            raise raises
        return dict(result or {})

    adapter.start_group_thread = stub
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    monkeypatch.setattr(module, "_live", (adapter, loop))
    return adapter


def test_group_message_dry_run_does_not_send(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    import json

    module = _load(monkeypatch, tmp_path)
    _live_tool(module, monkeypatch, raises=AssertionError("dry run must not reach the API"))
    out = json.loads(module._plow_start_group_message(
        {"recipients": ["+15550001111"], "body": "hi"}))
    assert out["success"] is True and out["dry_run"] is True
    assert out["would_send"]["recipient_count"] == 1


@pytest.mark.parametrize("recipients,message", [
    ([], "at least one recipient"),
    (["+1", "+1"], "duplicates"),
    # The comma is the delimiter: one array element carrying two addresses would
    # be approved as one recipient and delivered to two.
    (["+15550001111,+15559999999"], "may not contain a comma"),
])
def test_group_message_rejects_bad_recipients(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, recipients: list[str], message: str
) -> None:
    import json

    module = _load(monkeypatch, tmp_path)
    out = json.loads(module._plow_start_group_message(
        {"recipients": recipients, "body": "hi"}))
    assert out["success"] is False and message in out["error"]


@pytest.mark.parametrize("confirm", [False, "false", "no", "0", 0, None, "", "off", "maybe"])
def test_no_falsy_or_unparseable_confirm_value_can_authorize_a_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, confirm: Any
) -> None:
    """bool("false") is True, and a model emits that string for a declared bool.
    This is the only guard on the tool's one irreversible effect."""
    import json

    module = _load(monkeypatch, tmp_path)
    _live_tool(module, monkeypatch, raises=AssertionError("must not send"))
    out = json.loads(module._plow_start_group_message(
        {"recipients": ["+15550001111"], "body": "hi", "dry_run": False, "confirm": confirm}))
    assert out["success"] is False
    assert "confirm" in out["error"] and "nothing was sent" in out["error"]


@pytest.mark.parametrize("dry_run", ["false", "no", "0", 0, "off"])
def test_string_falsy_dry_run_is_a_real_send_not_a_silent_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, dry_run: Any
) -> None:
    import json

    module = _load(monkeypatch, tmp_path)
    sent: list[tuple[str, str]] = []
    _live_tool(module, monkeypatch, result={"chat_id": "cht_n", "adoption": "adopted"}, record=sent)
    out = json.loads(module._plow_start_group_message(
        {"recipients": ["+15550001111"], "body": "hi", "dry_run": dry_run, "confirm": True}))
    assert out["success"] is True and "dry_run" not in out
    assert len(sent) == 1


@pytest.mark.parametrize("junk", ["tru", "maybe"])
def test_unparseable_dry_run_stays_a_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, junk: str
) -> None:
    """Unrecognised input must fall to the direction that does nothing, and for
    dry_run that is True — otherwise a typo becomes the irreversible branch."""
    import json

    module = _load(monkeypatch, tmp_path)
    _live_tool(module, monkeypatch, raises=AssertionError("must not send"))
    out = json.loads(module._plow_start_group_message(
        {"recipients": ["+15550001111"], "body": "hi", "dry_run": junk, "confirm": True}))
    assert out["success"] is True and out["dry_run"] is True


def test_group_message_reports_adoption_separately_from_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A thread nobody is listening to is the bug this tool shipped with, so
    delivery must not read as reachability."""
    import json

    module = _load(monkeypatch, tmp_path)
    _live_tool(module, monkeypatch, result={
        "chat_id": "cht_new", "message_id": "m1", "delivery_status": "sent",
        "thread_handle": "+15550001111", "adoption": "not-on-this-agents-line"})
    out = json.loads(module._plow_start_group_message(
        {"recipients": ["+15550001111"], "body": "hi", "dry_run": False, "confirm": True}))
    assert out["success"] is True
    assert out["delivery_status"] == "sent"
    assert out["adoption"] == "not-on-this-agents-line"


def test_disconnected_gateway_sends_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    import json

    module = _load(monkeypatch, tmp_path)
    assert module._live is None
    out = json.loads(module._plow_start_group_message(
        {"recipients": ["+15550001111"], "body": "hi", "dry_run": False, "confirm": True}))
    assert out["success"] is False and "not connected" in out["error"]


async def test_connect_publishes_the_live_adapter_and_disconnect_retires_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The tool handler is synchronous and bridges onto the adapter's loop via
    `_live`; a connect that never publishes it leaves the tool permanently
    reporting a disconnected gateway."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    http = _HTTP()
    http.get = lambda url, headers: _Resp(  # type: ignore[attr-defined,method-assign]
        {"object": "list", "data": [_chat("cht_a")], "has_more": False}
    )
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda: http)

    async def listen_once() -> None: ...

    monkeypatch.setattr(adapter, "_listen", listen_once)
    await adapter.connect(is_reconnect=True)
    assert module._live is not None and module._live[0] is adapter
    await adapter._ws_task
    await adapter.disconnect()
    assert module._live is None


async def test_start_group_thread_posts_the_unversioned_send_and_reports_adoption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The thread-creation POST goes to the unversioned /channels/linq/send with
    the agent bearer, and adoption is judged by the refreshed grant — a response
    naming a sibling agent's thread must not make this gateway claim it."""
    import json as jsonlib

    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))

    posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    class _TextResp(_Resp):
        async def text(self) -> str:
            return jsonlib.dumps(self._payload)

    class _SendHTTP(_HTTP):
        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            posts.append((url, json, headers))
            return _TextResp({"chat_id": "cht_new", "message_id": "m1"})

        def get(self, url: str, *, headers: dict[str, str]) -> _Resp:
            if "/messages" in url:
                # _anchor's newest-first read on the adopted chat: the top row
                # is the message this send just created.
                return _Resp({"data": [{"uid": "msg_ours"}], "has_more": False})
            return _Resp({"object": "list", "data": [_chat("cht_a"), _chat("cht_new")], "has_more": False})

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda: _SendHTTP())
    data = await adapter.start_group_thread("+15550001111", "hello")

    assert posts == [(
        f"{module.BASE}/channels/linq/send",
        {"thread_handle": "+15550001111", "text": "hello"},
        adapter.auth,
    )]
    assert data["adoption"] == "adopted"
    assert adapter.chat_uids == frozenset({"cht_a", "cht_new"})
    # Adoption must BASELINE the new chat immediately, via the anchor read: the
    # send response's message_id is the provider id, never the `msg_` uid the
    # backfill cursor compares, so a reply landing before the next reconnect
    # would otherwise become the baseline and be silently skipped.
    assert adapter._anchored_chats.get("cht_new") is True
    assert adapter._load_checkpoint("cht_new") == "msg_ours"


async def test_a_lagging_disconnect_on_a_replaced_instance_keeps_the_live_one_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A stale adapter's disconnect must not clobber its successor's `_live`
    entry — that would leave the send tool reporting a disconnected gateway
    while a healthy adapter is live."""
    module = _load(monkeypatch, tmp_path)
    stale = module.PlowChatAdapter(SimpleNamespace(extra={}))
    live = module.PlowChatAdapter(SimpleNamespace(extra={}))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(module, "_live", (live, loop))

    await stale.disconnect()
    assert module._live == (live, loop)

    await live.disconnect()
    assert module._live is None


@pytest.mark.parametrize(("status", "retries"), [
    pytest.param(401, False, id="revoked_is_terminal"),
    # A 403 is resource-scoped (removed from one chat) and a 502 in front of
    # Plow is transient -- latching either as fatal is the bug #17's own
    # review caught. Widening the guard past 401 turns these rows red;
    # removing it turns the 401 row red.
    pytest.param(403, True, id="forbidden_keeps_retrying"),
    pytest.param(502, True, id="transient_keeps_retrying"),
])
async def test_ticket_mint_status_decides_terminal_vs_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, status: int, retries: bool
) -> None:
    """401 at the ticket mint is terminal, not a blip: every retry presents the
    same revoked credential (observed in production -- one WARNING a minute,
    line dead, adapter reporting itself connected). Everything else keeps
    warn-and-retry."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    calls: list[str] = []
    session = _Session(calls=calls)
    session.ticket_status = status
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: session)
    if retries:
        with mock.patch.object(module.asyncio, "sleep", side_effect=StopAsyncIteration):
            with pytest.raises(StopAsyncIteration):
                await adapter._listen()
    else:
        monkeypatch.setattr(module, "_live", (adapter, None))  # published, as connect() would have
        with mock.patch.object(module.asyncio, "sleep", side_effect=AssertionError("must not retry a revoked token")):
            await adapter._listen()  # returns; raising into the sleep would fail
        assert module._live is None, "a terminal stop must retire the tool handle"
    assert "ws_connect" not in calls, calls
