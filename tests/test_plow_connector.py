"""Behavior tests for the plow-connectors helper.

The helper's observable contract is the HTTP request it issues to the Plow
connector API and the response it returns, so the tests assert on the request
(method, URL, auth header, JSON body) and on the failure behavior — not on
internal structure.
"""
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "ref/hermes-skill/plow-connectors/plow_connector.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("plow_connector", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def captured(monkeypatch, mod):
    """Patch urlopen to capture the outgoing Request and return a canned body."""
    seen = {}

    class _Resp:
        def __init__(self, body):
            self._body = body.encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.method
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = req.data.decode() if req.data else None
        return _Resp('{"ok":true}')

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    return seen


def _clear_env(monkeypatch):
    for k in ("PLOW_CHAT_TOKEN", "PLOW_CONNECTOR_TOKEN", "PLOW_CHAT_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize(
    "env, args, expected",
    [
        pytest.param(
            {"PLOW_CHAT_TOKEN": "tok-abc"},
            ("gmail", "status"),
            {"method": "GET", "url": "https://api.plow.co/v1/connectors/gmail/status",
             "auth": "Bearer tok-abc", "body": None, "content_type": None},
            id="status-get-no-body",
        ),
        pytest.param(
            {"PLOW_CHAT_TOKEN": "tok-abc"},
            ("slack", "messages.send", '{"account":"T1","channel_id":"C1","text":"hi"}'),
            {"method": "POST", "url": "https://api.plow.co/v1/connectors/slack/messages.send",
             "auth": "Bearer tok-abc", "json": {"account": "T1", "channel_id": "C1", "text": "hi"},
             "content_type": "application/json"},
            id="action-post-json-body",
        ),
        pytest.param(
            {"PLOW_CHAT_TOKEN": "chat-tok", "PLOW_CONNECTOR_TOKEN": "conn-tok"},
            ("gmail", "status"),
            {"method": "GET", "auth": "Bearer conn-tok"},
            id="connector-token-overrides-chat-token",
        ),
        pytest.param(
            {"PLOW_CHAT_TOKEN": "tok-abc"},
            ("gmail", "messages.list"),
            {"method": "POST", "json": {}, "content_type": "application/json"},
            id="post-empty-body-defaults-to-empty-object",
        ),
    ],
)
def test_request_shape(monkeypatch, mod, captured, env, args, expected):
    _clear_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    mod.call(*args)

    if "method" in expected:
        assert captured["method"] == expected["method"]
    if "url" in expected:
        assert captured["url"] == expected["url"]
    if "auth" in expected:
        assert captured["headers"]["authorization"] == expected["auth"]
    if "json" in expected:
        assert json.loads(captured["body"]) == expected["json"]
    if "body" in expected:
        assert captured["body"] == expected["body"]
    if "content_type" in expected:
        assert captured["headers"].get("content-type") == expected["content_type"]


def test_base_url_default_and_chat_override(monkeypatch, mod, captured):
    _clear_env(monkeypatch)
    monkeypatch.setenv("PLOW_CHAT_TOKEN", "t")
    mod.call("gmail", "status")
    assert captured["url"] == "https://api.plow.co/v1/connectors/gmail/status"

    monkeypatch.setenv("PLOW_CHAT_BASE_URL", "https://example.test/")
    mod.call("gmail", "status")
    assert captured["url"] == "https://example.test/v1/connectors/gmail/status"


@pytest.mark.parametrize("action", ["status", "messages.list", "calendar.events.list", "messages.modify-labels", "connect-code"])
def test_real_actions_accepted(monkeypatch, mod, captured, action):
    monkeypatch.setenv("PLOW_CHAT_TOKEN", "t")
    mod.call("gmail", action)
    assert captured["url"].endswith(f"/{action}")


@pytest.mark.parametrize("action", ["messages/list", "..", "a?b=1", "/v1/me", "messages..list", ""])
def test_url_escaping_action_is_rejected(monkeypatch, mod, action):
    # A prompted agent must not be able to smuggle / ? or .. into the URL path.
    monkeypatch.setenv("PLOW_CHAT_TOKEN", "t")
    with pytest.raises(SystemExit):
        mod.call("gmail", action)


def test_unknown_connector_is_fatal(monkeypatch, mod):
    monkeypatch.setenv("PLOW_CHAT_TOKEN", "t")
    with pytest.raises(SystemExit):
        mod.call("dropbox", "status")


def test_missing_token_is_fatal(monkeypatch, mod):
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit):
        mod.call("gmail", "status")


def test_malformed_json_body_is_fatal(monkeypatch, mod):
    monkeypatch.setenv("PLOW_CHAT_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        mod.main(["gmail", "messages.list", "{not valid json"])
    assert exc.value.code == 1


def test_http_error_exits_nonzero(monkeypatch, mod):
    monkeypatch.setenv("PLOW_CHAT_TOKEN", "t")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"nope"}'))

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit) as exc:
        mod.main(["gmail", "status"])
    assert exc.value.code == 1
