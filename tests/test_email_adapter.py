"""The plow_email platform: the agent's own email line, as its own Hermes
platform on the chat adapter's transport (design §5).

Loaded through `test_adapter._load`, so the same `gateway.*` stubs and the
same package loader serve both platforms.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from test_adapter import (
    _HTTP,
    _SEND_ARGV,
    _capture_events,
    _chat,
    _envelope,
    _load,
    _mark_anchored,
    _settle,
)

ADDRESS = "elm@plow.co"
OWNER = ("Sam", "sam@example.com")


def _mail_chat(uid: str, *, group: bool = False) -> dict[str, Any]:
    """A gmail thread as the listing serves it (design §1): the line is the
    email line, the owner is on it, and any other address is a member."""
    participants = [
        {"type": "agent", "relationship": "self",
         "line": {"uid": "ln_mail", "provider_key": ADDRESS, "display_name": "Elm"}},
        {"type": "member", "uid": f"mem_owner_{uid}", "role": "owner",
         "display_name": OWNER[0], "provider_key": OWNER[1]},
    ]
    if group:
        participants.append({"type": "member", "uid": f"mem_other_{uid}", "role": "member",
                             "display_name": "Dana", "provider_key": "dana@example.com"})
    return {"uid": uid, "provider": "gmail", "display_name": "Re: invoice",
            "participants": participants, "trusted": False, "status": "active"}


def _load_email(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> tuple[Any, Any]:
    """The plugin, plus the registry entry `register` would have made for
    plow_email -- the object the gateway reads `platform_hint` from on every
    prompt build (agent/system_prompt.py `_platform_hint`)."""
    module = _load(monkeypatch, tmp_path)
    entry = SimpleNamespace(platform_hint=module.plow_email.hint())
    registry = types.ModuleType("gateway.platform_registry")
    registry.platform_registry = SimpleNamespace(get={"plow_email": entry}.__getitem__)
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", registry)
    return module, entry


def _adapter(module: Any) -> Any:
    return module.plow_email.PlowEmailAdapter(SimpleNamespace(extra={}))


async def test_reach_keeps_the_email_line_and_publishes_its_address_in_the_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """The hint is static per platform and the address is per agent, so the
    entry is written in place the first time reach reveals a gmail chat.
    Zero threads is the normal first state of a new line: reach holds
    empty, nothing is published, and the address-less hint stands -- no turn
    can arrive on this platform before a thread exists."""
    module, entry = _load_email(monkeypatch, tmp_path)
    assert entry.platform_hint == ("This is your own email line. Mail here is addressed to you; "
                                   "you write as yourself, at email length.")
    adapter = _adapter(module)
    adapter._set_reach([_chat("cht_a")])
    assert adapter._chats == {} and adapter._foreign == frozenset({"cht_a"})
    assert adapter.address is None
    assert entry.platform_hint.startswith("This is your own email line. ")

    adapter._set_reach([_chat("cht_a"), _mail_chat("cht_m"), _mail_chat("cht_n", group=True)])
    assert set(adapter._chats) == {"cht_m", "cht_n"} and adapter._foreign == frozenset({"cht_a"})
    assert adapter.address == ADDRESS
    assert entry.platform_hint == (f"This is your own email line, {ADDRESS}. Mail here is addressed "
                                   "to you; you write as yourself, at email length.")
    assert [(uid, (await adapter.get_chat_info(uid))["type"]) for uid in adapter._chats] == [
        ("cht_m", "dm"), ("cht_n", "group")]


@pytest.mark.parametrize(("group", "chat_type", "role"),
                         [(False, "dm", "owner"), (True, "group", "member")],
                         ids=["owner-dm", "member-group"])
async def test_a_gmail_thread_is_plow_emails_turn_and_never_plow_chats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture, group: bool, chat_type: str, role: str,
) -> None:
    """Both adapters hold the same grant and see the same frames. A gmail
    frame is a plow_email turn -- platform, chat_type and chat_id are the
    three fields upstream's build_session_key (gateway/session.py:641) joins
    into `<ns>:plow_email:<chat_type>:<chat_uid>` -- and the phone line's
    frame is not this platform's. The prompt is the owner fact and nothing
    else: no roster, no trust prose; the hint rides the platform entry. On a
    member's mail it is still the line's owner who is named, and only an
    owner's mail carries owner authority. A thread whose roster has no owner
    at all is the one shape that cannot be rendered: it must name itself on
    the way out, because the frame is already deduped and the mail is gone."""
    module, _entry = _load_email(monkeypatch, tmp_path)
    listing = [_chat("cht_a"), _mail_chat("cht_m", group=group)]
    chat = module.PlowChatAdapter(SimpleNamespace(extra={}))
    chat._set_reach(listing)
    _mark_anchored(chat, "cht_a")
    mail = _adapter(module)
    mail._set_reach(listing)
    chat_events, mail_events = _capture_events(monkeypatch, chat), _capture_events(monkeypatch, mail)

    frame = _envelope("evt_1", "cht_m", "msg_1", body="Can you send the invoice?", role=role)
    await chat._on_frame(frame, None)
    await _settle(chat)
    await mail._on_frame(frame, None)
    await mail._on_frame(frame, None)                       # a redelivered event is one turn
    await mail._on_frame(_envelope("evt_2", "cht_a", "msg_2"), None)

    assert chat_events == [], "the email line's turn is never the phone line's"
    [event] = mail_events
    source = event["source"]
    assert (source.platform, source.chat_type, source.chat_id) == ("plow_email", chat_type, "cht_m")
    assert source.role_authorized is (role == "owner") and source.user_id == f"mem_{role}_cht_m"
    assert event["text"] == "Can you send the invoice?" and event["message_id"] == "msg_1"
    assert event["channel_prompt"] == module._owner_fact(OWNER)

    mail._set_reach([{**_mail_chat("cht_x"), "participants": []}])
    with pytest.raises(RuntimeError, match="cht_x has no owner participant"):
        await mail._on_frame(_envelope("evt_3", "cht_x", "msg_3"), None)
    # `_serve` logs the TYPE only -- an aiohttp handshake error stringifies a
    # live ticket -- so an unnamed raise reads exactly like a network blip.
    assert "cht_x has no owner participant" in caplog.text


@pytest.mark.parametrize("role", ["owner", "member"])
async def test_an_email_turn_confines_the_chat_tools_and_never_sends_from_the_owners_gmail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, role: str,
) -> None:
    """The tools and the Latch mail gate read one turn slot. A member's email
    turn is refused the contact book like a member's chat turn; and on ANY
    email turn a plow-gog send is blocked -- the reply goes out from this
    line, which is the ghostwriting bug this design exists to end."""
    module, _entry = _load_email(monkeypatch, tmp_path)
    mail = _adapter(module)
    event = SimpleNamespace(source=SimpleNamespace(chat_id="cht_m", chat_type="dm",
                                                   role_authorized=role == "owner"))
    await mail.on_processing_start(event)
    assert module._ACTIVE_TURN.get() == {"chat_uid": "cht_m", "owner": role == "owner", "dm": False}
    contacts = json.loads(module._plow_contacts({}))
    assert contacts["success"] is False
    assert ("member's turn" in contacts["error"]) == (role == "member")
    gate = module._pre_tool_call("mcp__latch__plow_run_command", {"argv": _SEND_ARGV}, session_id="s1")
    assert gate["action"] == "block"
    await mail.on_processing_complete(event, None)
    assert module._ACTIVE_TURN.get() is None
