"""The seeded skills may only name tools this plugin registers.

The invite skill shipped for days naming three tools that never existed; the
model read `plow_prepare_invite`, found nothing, and the invite never went
out. Every `plow_*` token in a seed skill is checked against `register`."""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from test_adapter import _load

ROOT = pathlib.Path(__file__).resolve().parents[1]
# google-workspace is excluded on purpose: its `plow_*` tokens name the Latch
# relay MCP server's tools, not this plugin's, so this plugin cannot register them.
SKILLS = sorted(s for s in (ROOT / "seed-skills").rglob("SKILL.md")
                if s.parent.name != "google-workspace")


def _registered_tools(module: Any) -> set[str]:
    names: set[str] = set()
    ctx = SimpleNamespace(
        llm=None,
        deferred_questions=None,
        register_platform=lambda **kw: None,
        register_tool=lambda **kw: names.add(kw["name"]),
        register_hook=lambda *a, **kw: None,
    )
    module.register(ctx)
    return names


@pytest.mark.parametrize("skill", SKILLS, ids=[s.parent.name for s in SKILLS])
def test_a_seed_skill_names_only_tools_the_plugin_registers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, skill: pathlib.Path
) -> None:
    named = set(re.findall(r"\bplow_[a-z_]+\b", skill.read_text()))
    registered = _registered_tools(_load(monkeypatch, tmp_path))
    assert named, f"{skill} names no plow_* tool; drop it from this check on purpose if that is right"
    assert named <= registered, f"{skill} names tools the plugin does not register: {sorted(named - registered)}"
