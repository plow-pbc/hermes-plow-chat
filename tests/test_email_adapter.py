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
