import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class Platform(str):
    pass


class SendResult:
    def __init__(self, success=False, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


class MessageType:
    TEXT = "text"


class MessageEvent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class BasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self.is_connected = False

    def build_source(self, **kwargs):
        return types.SimpleNamespace(**kwargs, platform=self.platform)

    def truncate_message(self, body):
        return [body]

    def _mark_connected(self):
        self.is_connected = True

    def _mark_disconnected(self):
        self.is_connected = False

    def _set_fatal_error(self, *args, **kwargs):
        self.fatal_error = (args, kwargs)

    async def _notify_fatal_error(self):
        self.fatal_notified = True


sys.modules.setdefault("gateway", types.ModuleType("gateway"))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
config_mod = types.ModuleType("gateway.config")
config_mod.Platform = Platform
sys.modules["gateway.config"] = config_mod
platforms_mod = types.ModuleType("gateway.platforms")
sys.modules["gateway.platforms"] = platforms_mod
base_mod = types.ModuleType("gateway.platforms.base")
base_mod.BasePlatformAdapter = BasePlatformAdapter
base_mod.MessageEvent = MessageEvent
base_mod.MessageType = MessageType
base_mod.SendResult = SendResult
sys.modules["gateway.platforms.base"] = base_mod

ADAPTER_PATH = Path(__file__).resolve().parents[1] / "ref" / "hermes-plugin" / "plow_chat" / "adapter.py"
spec = importlib.util.spec_from_file_location("plow_chat_adapter_under_test", ADAPTER_PATH)
adapter_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter_mod)


class DummyConfig:
    extra = {"chat_uid": "cht_test", "token": "token_test"}


class RecordingAdapter(adapter_mod.PlowChatAdapter):
    def __init__(self, monkeypatch):
        monkeypatch.delenv("PLOW_CHAT_CHAT_UID", raising=False)
        monkeypatch.delenv("PLOW_CHAT_TOKEN", raising=False)
        super().__init__(DummyConfig())
        self.sent = []
        self.handled = []
        self.send_success = True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SendResult(success=self.send_success, message_id="msg_welcome")

    async def handle_message(self, event):
        self.handled.append(event)


def test_chat_active_sends_default_welcome(monkeypatch):
    monkeypatch.delenv("PLOW_CHAT_WELCOME_MESSAGE", raising=False)
    adapter = RecordingAdapter(monkeypatch)

    asyncio.run(adapter._handle_ws_frame({"type": "chat_active"}))

    assert adapter.sent == [
        (
            "cht_test",
            "Hi — Plow Chat is connected to Hermes now. Reply here to start chatting.",
        )
    ]


def test_inbound_message_auto_approves_verified_sender(monkeypatch):
    approved = []

    class FakeStore:
        _lock = None

        def approve_user(self, platform, user_id, user_name=""):
            approved.append((platform, user_id, user_name))

    class Lock:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    FakeStore._lock = Lock()

    pairing_mod = types.ModuleType("gateway.pairing")
    pairing_mod.PairingStore = FakeStore
    monkeypatch.setitem(sys.modules, "gateway.pairing", pairing_mod)

    adapter = RecordingAdapter(monkeypatch)
    frame = {
        "type": "message_received",
        "message": {
            "uid": "msg_1",
            "direction": "inbound",
            "body": "hello",
            "sender": {"uid": "cp_member", "display_name": "Patrick"},
        },
    }

    asyncio.run(adapter._handle_ws_frame(frame))

    assert approved == [("plow_chat", "cp_member", "Patrick")]
    assert adapter.handled[0].source.user_id == "cp_member"


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self.payload


class RecordingSession:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.payload)

    async def close(self):
        return None


def test_send_uses_bearer_token(monkeypatch):
    adapter = RecordingAdapter(monkeypatch)
    session = RecordingSession({"uid": "msg_1", "status": "sent"})
    # send() opens a fresh per-call aiohttp.ClientSession (see adapter.send),
    # so stub the constructor to hand back the recording session.
    monkeypatch.setattr(sys.modules["aiohttp"], "ClientSession", lambda *a, **k: session, raising=False)

    result = asyncio.run(adapter_mod.PlowChatAdapter.send(adapter, "cht_test", "hello"))

    assert result.success is True
    assert session.posts == [
        (
            "https://api.plow.co/v1/chats/cht_test/messages",
            {
                "json": {"body": "hello"},
                "headers": {"Authorization": "Bearer token_test"},
            },
        )
    ]


def test_ws_ticket_is_scoped_to_chat_and_uses_bearer(monkeypatch):
    adapter = RecordingAdapter(monkeypatch)
    session = RecordingSession({"ticket": "wst_test"})
    adapter._http_session = session

    ticket = asyncio.run(adapter._mint_ws_ticket())

    assert ticket == "wst_test"
    assert session.posts == [
        (
            "https://api.plow.co/v1/ws/ticket",
            {
                "json": {"chat_id": "cht_test"},
                "headers": {"Authorization": "Bearer token_test"},
            },
        )
    ]


SENT = [("cht_test", "ready!")]


@pytest.mark.parametrize(
    "send_success, frames, expected_sent",
    [
        # chat_active sends the welcome once; a duplicate chat_active does not re-send.
        (True, ["chat_active", "chat_active"], SENT),
        # An ambiguous send (committed server-side, reported as a client failure)
        # latches on attempt, so a later chat_active does not re-send.
        (False, ["chat_active", "chat_active"], SENT),
        # connected frames never trigger the welcome (no first-connect path).
        (True, ["connected", "connected"], []),
    ],
    ids=["chat-active-once", "ambiguous-send-latched", "connected-no-welcome"],
)
def test_activation_welcome_latch(monkeypatch, send_success, frames, expected_sent):
    monkeypatch.setenv("PLOW_CHAT_WELCOME_MESSAGE", "ready!")
    adapter = RecordingAdapter(monkeypatch)
    adapter.send_success = send_success

    for frame in frames:
        asyncio.run(adapter._handle_ws_frame({"type": frame}))

    assert adapter.sent == expected_sent
