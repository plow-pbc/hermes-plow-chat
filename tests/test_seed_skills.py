"""The seeded skills may only name tools this plugin registers.

The invite skill shipped for days naming three tools that never existed; the
model read `plow_prepare_invite`, found nothing, and the invite never went
out. Every `plow_*` token in every seed skill is checked against `register`,
except the Latch relay MCP server's own tools, which this plugin does not and
cannot register."""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from test_adapter import _load

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "seed-skills").rglob("SKILL.md"))
# Served by the Latch relay MCP server the agent connects to, not by this plugin.
LATCH_MCP_TOOLS = {"plow_list_skills", "plow_read_skill"}


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
    tokens = set(re.findall(r"\bplow_[a-z_]+\b", skill.read_text()))
    registered = _registered_tools(_load(monkeypatch, tmp_path))
    # A skill naming nothing at all means the read or the pattern broke, not
    # that the skill is clean. Checked before the allowlist comes off, because
    # a skill whose whole plow_* surface is Latch's (google-workspace) is
    # legitimately empty afterwards.
    assert tokens, f"{skill} names no plow_* token at all; the read or the pattern is broken"
    named = tokens - LATCH_MCP_TOOLS
    assert named <= registered, f"{skill} names tools the plugin does not register: {sorted(named - registered)}"
