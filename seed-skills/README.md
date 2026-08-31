# Seed skills — public mirror

`plow-pbc/agent-mgr` pins every artifact it installs to a repo + SHA in its
`runtime/stack.json`. These two skills used to be pinned to `plow-pbc/plow`,
which is private, so any restore run by someone outside the org died on a
GitHub 404 before the agent came up. They live here so that an anonymous
clone can complete a restore.

Verbatim copies, no edits, taken from `plow-pbc/plow` at commit
`68ebea369d5daa1a6be987e4152f0a15009f4800`:

| here | in `plow-pbc/plow` |
| --- | --- |
| `growth/plow-invite` | `cloud-agents/hermes/image/seed/skills/growth/plow-invite` |
| `productivity/google-workspace` | `cloud-agents/hermes/image/seed/skills/productivity/google-workspace` |

`plow-invite/SKILL.md` says "converge, don't fork" — that still holds, and
these copies are the reason it now needs saying out loud. The hosted-agent
image still bakes from `plow-pbc/plow`. Change a skill there first, copy the
result here, then bump the SHA in `agent-mgr`'s `runtime/stack.json`. Editing
only this copy silently splits the two runtimes.
