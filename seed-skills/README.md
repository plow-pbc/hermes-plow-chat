# Seed skills

The two skills every Plow agent is seeded with. **This directory is the
canonical copy.**

| skill | mirrored into |
| --- | --- |
| `growth/plow-invite` | `plow-pbc/plow-hermes-agent` at `image/seed/skills/growth/plow-invite` (the base image bakes it) |
| `productivity/google-workspace` | `plow-pbc/plow-hermes-agent` at `image/seed/skills/productivity/google-workspace` |

Change a skill here first, then copy the result verbatim into the base image
and open that PR; `tests/test_seed_skills.py` holds every `plow_*` tool a skill
names to the set this plugin registers. `plow-pbc/agent-mgr` (deprecated)
still pins these paths by SHA in its `runtime/stack.json`. The former source
under `plow-pbc/plow` `cloud-agents/hermes/` no longer exists.
