"""Unit coverage for the plow_chat adapter's startup baseline and multi-chat behavior.

The adapter runs inside hermes, so `gateway.*` is stubbed below and the module
is loaded straight from `plow-chat-platform/__init__.py` — these tests exercise
the adapter without adding Hermes itself as a dependency.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
    import enum

    class _MessageType(enum.Enum):
        TEXT = "text"; PHOTO = "photo"; VIDEO = "video"; VOICE = "voice"; DOCUMENT = "document"

    base.MessageType = _MessageType  # type: ignore[attr-defined]
    cache = tmp_path / "cache"
    cache.mkdir()

    def _cache(kind: str):
        def write(data: bytes, name: str = "") -> str:
            path = cache / f"{kind}_{len(list(cache.iterdir()))}{name}"
            path.write_bytes(data)
            return str(path)
        return write

    def _cache_doc():
        # Distinguishable from the image/audio/video stubs above: the second
        # arg here is a real filename, not a bare extension suffix.
        def write(data: bytes, filename: str = "") -> str:
            path = cache / f"doc_{len(list(cache.iterdir()))}_{filename}"
            path.write_bytes(data)
            return str(path)
        return write

    base.cache_image_from_bytes = _cache("img")  # type: ignore[attr-defined]
    base.cache_audio_from_bytes = _cache("aud")  # type: ignore[attr-defined]
    base.cache_video_from_bytes = _cache("vid")  # type: ignore[attr-defined]
    base.cache_document_from_bytes = _cache_doc()  # type: ignore[attr-defined]

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
    # Zero window and no retry pause under test: a burst still hands off on the
    # chat's own task, so a test awaits `_settle` where it needs the turn landed.
    module.INBOUND_DEBOUNCE_SECONDS = 0
    module.HAND_OFF_RETRY_SECONDS = 0
    return module


def _capture_events(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> list[Any]:
    """Stand in for hermes: every hand-off lands here."""
    events: list[Any] = []

    async def capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)
    return events


async def _settle(adapter: Any) -> None:
    """Let every chat's server hand off what it holds."""
    await asyncio.gather(*(queue.join() for queue, _server in adapter._inbound.values()))


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


def _envelope(
    event_id: str,
    chat_id: str,
    message_id: str,
    *,
    role: str = "owner",
    body: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "message_received",
        "chat_id": chat_id,
        "data": {
            "type": "message_received",
            "message": {
                "uid": message_id,
                "body": message_id if body is None else body,
                "attachments": attachments or [],
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


class _BytesResp(_Resp):
    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(None, status)
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _ContentHTTP:
    """GET on a signed content URL; records that no bearer header was sent."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.gets: list[tuple[str, dict[str, str] | None]] = []

    def get(self, url: str, **kw: Any) -> _BytesResp:
        self.gets.append((url, kw.get("headers")))
        return _BytesResp(b"\x89PNG", status=self.status)

    async def __aenter__(self) -> "_ContentHTTP":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...


URL = "/v1/chats/cht_a/attachments/att_photo/content?exp=1&sig=2"


def _attachment(**overrides: Any) -> dict[str, Any]:
    """One inbound part with every key the Plow contract always sends."""
    return {"uid": "att_photo", "filename": "photo.png", "content_type": "image/png",
            "url": URL, "size_bytes": 4, "status": "received",
            "url_expires_at": "2026-08-28T00:05:00Z"} | overrides


@pytest.mark.parametrize(
    ("body", "content_type", "url", "status", "expected_text", "expected_kind"),
    [
        ("", "image/png", URL, 200, "(attachment)", "photo"),
        ("Photo attached", "image/png", URL, 200, "Photo attached", "photo"),
        ("", "audio/x-m4a", URL, 200, "(attachment)", "voice"),
        ("", "video/mp4", URL, 200, "(attachment)", "video"),
        ("", "application/pdf", URL, 200, "(attachment)", "document"),
        # status "failed" carries url: null by contract — surfaced, not dropped.
        ("", "image/png", None, 200, "[attachment: image/png delivery failed]", "text"),
        # provider bytes gone (404 from the content route) — surfaced, not dropped.
        ("", "image/png", URL, 404, "[attachment: image/png unavailable]", "text"),
        # null content_type falls to application/octet-stream, becomes document.
        ("", None, URL, 200, "(attachment)", "document"),
        # a content-type parameter (charset, boundary, ...) must be stripped.
        ("", "image/jpeg; charset=binary", URL, 200, "(attachment)", "photo"),
    ],
    ids=["media-only", "captioned", "audio", "video", "document", "failed-part", "fetch-failed", "null-type",
         "parameterized-type"],
)
async def test_inbound_media_reaches_hermes_as_local_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    body: str,
    content_type: str,
    url: str | None,
    status: int,
    expected_text: str,
    expected_kind: str,
) -> None:
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    http = _ContentHTTP(status=status)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: http)
    handled = _capture_events(monkeypatch, adapter)

    expected_type = (content_type.split(";")[0].strip() if content_type
                     else "application/octet-stream")
    await adapter._on_frame(_envelope(
        "evt_media", "cht_a", "msg_media", body=body,
        attachments=[_attachment(content_type=content_type, url=url)],
    ))
    await _settle(adapter)

    event = handled[0]
    assert event["text"] == expected_text
    assert event["message_type"].value == expected_kind
    if expected_kind == "text":
        assert event["media_urls"] == []
        return
    assert http.gets == [(module.BASE + URL, None)], "signed URL, no bearer header"
    (path,) = event["media_urls"]
    assert pathlib.Path(path).read_bytes() == b"\x89PNG"
    assert event["media_types"] == [expected_type]
    if expected_kind == "document":
        assert pathlib.Path(path).name.endswith("photo.png"), "cached document keeps its filename"


async def test_inbound_multi_attachment_keeps_good_parts_and_notes_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A Plow-failed part (status "failed", url null) is named in the text and
    logged, without dropping the good part that arrived alongside it."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    http = _ContentHTTP(status=200)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: http)
    handled = _capture_events(monkeypatch, adapter)

    with caplog.at_level(logging.WARNING):
        await adapter._on_frame(_envelope(
            "evt_multi", "cht_a", "msg_multi", body="",
            attachments=[
                _attachment(uid="att_ok"),
                _attachment(uid="att_bad", filename="doc.pdf", content_type="application/pdf",
                            url=None, status="failed", url_expires_at=None),
            ],
        ))

    await _settle(adapter)
    event = handled[0]
    assert len(event["media_urls"]) == 1
    assert event["media_types"] == ["image/png"]
    assert event["message_type"].value == "photo"
    assert event["text"] == "[attachment: application/pdf delivery failed]"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("att_bad" in r.getMessage() for r in warnings)
    assert not any("sig=" in r.getMessage() or "http" in r.getMessage() for r in warnings)


async def test_duplicate_delivery_does_not_refetch(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The same message uid arriving twice (socket/backfill overlap, or two
    distinct wrapping events) must be deduped before the attachment fetch."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    http = _ContentHTTP(status=200)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: http)
    handled = _capture_events(monkeypatch, adapter)

    attachments = [_attachment()]
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_dup", attachments=attachments))
    await adapter._on_frame(_envelope("evt_2", "cht_a", "msg_dup", attachments=attachments))
    await _settle(adapter)

    assert len(handled) == 1
    assert len(http.gets) == 1


async def test_a_burst_carries_every_part_s_media(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The motivating split: a caption bubble, then the photo, then a second
    photo. One turn, both files, in arrival order, typed from the whole burst."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    http = _ContentHTTP(status=200)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: http)
    handled = _capture_events(monkeypatch, adapter)
    module.INBOUND_DEBOUNCE_SECONDS = 0.05   # the fetch yields; a zero window would close on it
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_1", body="look at these"))
    await adapter._on_frame(_envelope("evt_2", "cht_a", "msg_2", body="",
                                      attachments=[_attachment(uid="att_1")]))
    await adapter._on_frame(_envelope("evt_3", "cht_a", "msg_3", body="",
                                      attachments=[_attachment(uid="att_2", filename="two.png")]))
    await _settle(adapter)

    [event] = handled
    assert event["text"] == "look at these"
    assert len(event["media_urls"]) == 2 and event["media_types"] == ["image/png", "image/png"]
    assert event["message_type"].value == "photo"
    assert event["message_id"] == "msg_3"


async def test_a_slow_preview_fetch_does_not_split_the_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The preview bubble's bytes can take longer to fetch than the window
    lasts. It joined the burst the moment it arrived; the fetch is the
    hand-off's to wait for, not the window's."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    release = asyncio.Event()

    async def slow_fetch(item: Any, kind: str) -> str:
        await release.wait()
        return "/cache/preview.png"

    monkeypatch.setattr(module, "_fetch_attachment", slow_fetch)
    handled = _capture_events(monkeypatch, adapter)
    module.INBOUND_DEBOUNCE_SECONDS = 0.05
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_1", body="see this"))
    await adapter._on_frame(_envelope("evt_2", "cht_a", "msg_2", body="", attachments=[_attachment()]))
    release.set()
    await _settle(adapter)

    [event] = handled
    assert (event["text"], event["media_urls"], event["message_id"]) == ("see this", ["/cache/preview.png"], "msg_2")


@pytest.mark.parametrize(
    "second_role,turns",
    [
        ("owner", [("msg_2", "msg_1\n\nmsg_2")]),
        ("member", [("msg_1", "msg_1"), ("msg_2", "msg_2")]),
    ],
    ids=["same sender -> one turn", "another sender -> its own turn"],
)
async def test_a_burst_from_one_sender_is_one_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    second_role: str,
    turns: list[tuple[str, str]],
) -> None:
    """iMessage splits one intent into bubble + link preview; a person sends
    two lines in a row. Both used to reach hermes as separate turns — the
    second interrupting the first. Inside the window they are one turn whose
    ack is the LAST uid, so a restart mid-burst backfills the whole burst."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a", group=True)])
    handled = _capture_events(monkeypatch, adapter)
    module.INBOUND_DEBOUNCE_SECONDS = 0.05
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_1"))
    await adapter._on_frame(_envelope("evt_2", "cht_a", "msg_2", role=second_role))
    await adapter._on_frame(_envelope("evt_2_again", "cht_a", "msg_2", role=second_role))
    await _settle(adapter)

    assert [(event["message_id"], event["text"]) for event in handled] == turns
    assert (tmp_path / "plow_chat_last_uid").read_text() == "msg_2"
    # A late duplicate of a handed-off message is dropped, not a new turn.
    await adapter._on_frame(_envelope("evt_1_late", "cht_a", "msg_1"))
    await _settle(adapter)
    assert len(handled) == len(turns)


async def test_a_change_of_speaker_closes_the_burst_and_order_holds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A1, B2, A3 inside one window must reach hermes in that order — never
    B2 then "A1 A3". A change of speaker hands off what came before, and a
    slow earlier hand-off still acks before a fast later one."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a", group=True)])
    a1_entered, release_a1 = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def turn(event: Any) -> None:
        if event.text == "A1":
            a1_entered.set()
            await release_a1.wait()
        order.append(event.text)

    monkeypatch.setattr(adapter, "handle_message", turn)
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_1", body="A1"))
    await adapter._on_frame(_envelope("evt_2", "cht_a", "msg_2", body="B2", role="member"))
    await a1_entered.wait()
    await adapter._on_frame(_envelope("evt_3", "cht_a", "msg_3", body="A3"))
    assert order == [], "B2 waits behind A1"
    release_a1.set()
    await _settle(adapter)

    assert order == ["A1", "B2", "A3"]
    assert (tmp_path / "plow_chat_last_uid").read_text() == "msg_3"


async def test_a_backfilled_duplicate_of_an_in_flight_uid_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A socket drop mid-hand-off: the uid is unacked, so the reconnect's
    backfill pages it again while the first hand-off is still in flight. The
    chat's server delivers in order, so the duplicate reaches it after the
    ack and is dropped — hermes never sees the message twice."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    entered, release = asyncio.Event(), asyncio.Event()
    handled: list[str] = []

    async def slow_turn(event: Any) -> None:
        entered.set()
        await release.wait()
        handled.append(event.message_id)

    monkeypatch.setattr(adapter, "handle_message", slow_turn)
    frame = _envelope("evt_1", "cht_a", "msg_1")
    await adapter._on_frame(frame)
    await entered.wait()                     # the hand-off is in flight, unacked
    await adapter._backfill(_Session(backfill=[frame["data"]["message"]]), "cht_a")
    release.set()
    await _settle(adapter)

    assert handled == ["msg_1"]
    assert (tmp_path / "plow_chat_last_uid").read_text() == "msg_1"


async def test_a_failed_hand_off_is_retried_at_the_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A hand-off that fails is retried where it sits; everything behind it
    in the chat waits, so nothing ever acks past a message hermes never
    accepted, and order holds through the retry. Its media was fetched once:
    a retry must not go back to a signed url that may have expired."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    http = _ContentHTTP(status=200)
    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: http)
    handled: list[str] = []

    async def flaky(event: Any) -> None:
        if not handled:
            handled.append("boom")
            raise RuntimeError("hermes hiccup")
        handled.append(event.text)

    monkeypatch.setattr(adapter, "handle_message", flaky)
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_1", attachments=[_attachment()]))
    await adapter._on_frame(_envelope("evt_2", "cht_a", "msg_2", role="member"))
    await _settle(adapter)

    assert handled == ["boom", "msg_1", "msg_2"]
    assert len(http.gets) == 1
    assert (tmp_path / "plow_chat_last_uid").read_text() == "msg_2"


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
    await _settle(adapter)
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

    handled = _capture_events(monkeypatch, adapter)
    with caplog.at_level(logging.WARNING):
        await adapter._on_frame(_envelope("evt_a", "cht_a", "msg_a"))
        await adapter._on_frame(_envelope("evt_b", "cht_b", "msg_b_owner"))
        await adapter._on_frame(_envelope("evt_b", "cht_b", "msg_duplicate"))
        await adapter._on_frame(_envelope("evt_out", "cht_c", "msg_out"))
        await adapter._on_frame(_envelope("evt_b_member", "cht_b", "msg_b_member", role="member"))
        await _settle(adapter)

    # Chats hand off independently; only the order WITHIN a chat is a contract.
    handled.sort(key=lambda event: event["source"]["chat_id"])
    assert [(event["source"]["chat_id"], event["message_id"]) for event in handled] == [
        ("cht_a", "msg_a"),
        ("cht_b", "msg_b_owner"),
        ("cht_b", "msg_b_member"),
    ]
    owner_source, member_source = handled[1]["source"], handled[2]["source"]
    assert (owner_source["chat_name"], owner_source["chat_type"]) == ("Project room (cht_b)", "group")
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


async def test_concurrent_discovery_of_a_new_chat_greets_it_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The startup loop and a tool adoption can find the same brand-new chat at
    once; _ensure_anchor's lock, re-checked inside, is what keeps the
    disclosure wave to one send (and one checkpoint write)."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a")])

    class _YieldingResp(_Resp):
        async def __aenter__(self) -> "_Resp":
            # A reach rebuild lands mid-anchor, then control passes to the
            # racing discoverer -- the two interleavings the lock must absorb.
            adapter._set_reach([_chat("cht_a")])
            await asyncio.sleep(0)
            return self

    class _HTTPStub:
        def get(self, url: str, *, headers: dict[str, str]) -> _Resp:
            return _YieldingResp({"data": [{"uid": "msg_1"}], "has_more": False})

    sends: list[str] = []

    async def send(chat_id: str, content: str, **kwargs: Any) -> _SendResult:
        sends.append(chat_id)
        return _SendResult(success=True)

    monkeypatch.setattr(adapter, "send", send)
    http = _HTTPStub()

    async def discover() -> None:
        await adapter._ensure_anchor(http, "cht_a")

    await asyncio.gather(discover(), discover())
    assert sends == ["cht_a"], "concurrent discovery double-sent the disclosure wave"
    assert adapter._load_checkpoint("cht_a") == "msg_1"


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
    await _settle(adapter)                   # same sender: settle, or it is one turn
    await adapter._on_frame(_envelope("evt_no_reply", "cht_b", "msg_no_reply", role="member"))
    await _settle(adapter)
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


@pytest.mark.parametrize(
    ("method", "fail_at", "status"),
    [
        ("send_image_file", None, 200),
        ("send_voice", None, 200),
        ("send_video", None, 200),
        ("send_document", None, 200),
        ("send_image_file", "declare", 415),
        ("send_image_file", "upload", 403),
    ],
    ids=["image", "voice", "video", "document", "declare-415", "upload-403"],
)
async def test_outbound_media_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, method: str, fail_at: str | None, status: int,
) -> None:
    """Declare (bearer) -> PUT bytes to the provider URL with exactly the
    returned headers (no bearer) -> message POST (bearer). A non-2xx at the
    declare or the upload never reaches the message POST, so no attachment_uid
    ever points at a half-sent file."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    photo = tmp_path / "map.png"
    photo.write_bytes(b"\x89PNG")
    calls: list[tuple[str, str, Any, dict[str, str] | None]] = []
    upload_url = "https://uploads.example/put?sig=x"

    class _MediaHTTP:
        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
            calls.append(("POST", url, json, headers))
            if url.endswith("/attachments"):
                return _Resp({"uid": "att_1", "upload_url": upload_url,
                              "upload_headers": {"Content-Type": "image/png", "Content-Length": "4"}},
                             status=status if fail_at == "declare" else 200)
            return _Resp({"uid": "msg_sent"})

        def put(self, url: str, *, data: bytes, headers: dict[str, str]) -> _Resp:
            calls.append(("PUT", url, data, headers))
            return _Resp({}, status=status if fail_at == "upload" else 200)

        async def __aenter__(self) -> "_MediaHTTP":
            return self

        async def __aexit__(self, *exc: Any) -> None: ...

    monkeypatch.setattr(module.aiohttp, "ClientSession", lambda *a, **k: _MediaHTTP())

    result = await getattr(adapter, method)("cht_a", str(photo), caption="here")
    refused = await getattr(adapter, method)("cht_zzz", str(photo))

    assert not refused.success, "an ungranted chat sends nothing"
    declare = ("POST", f"{module.BASE}/v1/chats/cht_a/attachments",
               {"filename": "map.png", "content_type": "image/png", "size_bytes": 4}, adapter.auth)
    upload = ("PUT", upload_url, b"\x89PNG", {"Content-Type": "image/png", "Content-Length": "4"})
    send = ("POST", f"{module.BASE}/v1/chats/cht_a/messages",
            {"body": "here", "attachment_uids": ["att_1"]}, adapter.auth)
    if fail_at is None:
        assert result.success and result.message_id == "msg_sent"
        assert calls == [declare, upload, send]
    else:
        assert result.success is False and str(status) in result.error
        assert calls == ([declare] if fail_at == "declare" else [declare, upload])


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
    # A retired adapter must not keep serving a chat: its replacement's
    # backfill replays what this one still held, and two servers on one
    # chat would hand off twice and race the checkpoint.
    await adapter._on_frame(_envelope("evt_1", "cht_a", "msg_1"))
    _queue, server = adapter._inbound["cht_a"]
    await adapter.disconnect()
    assert module._live is None
    assert server.cancelled() or server.cancelling()
    assert not adapter._inbound


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

    # The linq send, then the first-meeting 👋 the adoption anchor fires --
    # the greeting rides the anchor, so a tool-created chat is disclosed even
    # though the socket is already up.
    assert posts == [(
        f"{module.BASE}/channels/linq/send",
        {"thread_handle": "+15550001111", "text": "hello"},
        adapter.auth,
    ), (
        f"{module.BASE}/v1/chats/cht_new/messages",
        {"body": "👋"},
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


# ---------------------------------------------------------------------------
# Naming: publish granted-thread titles into the image's alias registry
# (re-port of #14's naming slice onto the credential-scope adapter)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chat,expected", [
    # Plow's own answer -- the iMessage thread title -- always uid-suffixed.
    (_chat("cht_x", name="Snoqualmie Cabin Cleaning Thread"),
     "Snoqualmie Cabin Cleaning Thread (cht_x)"),
    # Sparse: absent, empty, and whitespace all mean "nobody titled it".
    (_chat("cht_x"), "cht_x"),
    (_chat("cht_x", name=""), "cht_x"),
    (_chat("cht_x", name="   "), "cht_x"),
])
def test_a_chat_is_named_from_its_own_title_or_uid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    chat: dict[str, Any],
    expected: str,
) -> None:
    module = _load(monkeypatch, tmp_path)
    assert module._resolve_chat_names([chat], "cht_home")[chat["uid"]] == expected


def test_the_home_chat_keeps_its_name_and_no_title_can_take_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The home is the one fixed, unsuffixed name. A title is chosen by whoever
    is in the thread, so the uid suffix is what makes a title incapable of
    equalling another room's name -- including the home's."""
    module = _load(monkeypatch, tmp_path)
    chats = [_chat("cht_home", name="Renamed By Someone"),
             _chat("cht_impostor", name="Plow Chat")]
    names = module._resolve_chat_names(chats, "cht_home")
    assert names["cht_home"] == "Plow Chat"
    assert names["cht_impostor"] == "Plow Chat (cht_impostor)"


def test_publishing_names_owns_one_key_and_leaves_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The file is shared with every other platform on the gateway: replace our
    key wholesale (stale entries are how names rot) and touch nothing else."""
    module = _load(monkeypatch, tmp_path)
    path = tmp_path / "channel_aliases.json"
    path.write_text(json.dumps({"telegram": {"1": "Ops"},
                                "plow_chat": {"cht_stale": "Old Name"}}))
    module._write_channel_aliases({"cht_a": "Cleaning (cht_a)"})
    assert json.loads(path.read_text()) == {
        "telegram": {"1": "Ops"},
        "plow_chat": {"cht_a": "Cleaning (cht_a)"},
    }


def test_a_corrupt_alias_file_is_not_clobbered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    module = _load(monkeypatch, tmp_path)
    path = tmp_path / "channel_aliases.json"
    path.write_text("[]")
    with pytest.raises(ValueError):
        module._write_channel_aliases({"cht_a": "Cleaning (cht_a)"})
    assert path.read_text() == "[]"


def test_reach_publishes_the_names_it_resolved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Writing the registry is the whole feature: the image re-applies the
    overlay on every directory build and load, which is what makes a granted
    thread addressable as plow_chat:#<name> before it has ever spoken."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    adapter._set_reach([_chat("cht_a"), _chat("cht_b", name="Cleaning", group=True)])
    data = json.loads((tmp_path / "channel_aliases.json").read_text())
    assert data["plow_chat"] == {"cht_a": "Plow Chat", "cht_b": "Cleaning (cht_b)"}


def test_an_unwritable_registry_does_not_cost_the_subscription(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Naming is cosmetic where reach is the credential grant. A registry that
    cannot be written must not fail _set_reach, or one read-only file tears
    down the line it decorates."""
    module = _load(monkeypatch, tmp_path)
    adapter = module.PlowChatAdapter(SimpleNamespace(extra={}))
    monkeypatch.setattr(module, "_write_channel_aliases",
                        mock.Mock(side_effect=OSError("read-only volume")))
    adapter._set_reach([_chat("cht_a")])
    assert adapter.chat_uids == frozenset({"cht_a"})
