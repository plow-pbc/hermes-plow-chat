# hermes-plow-chat

The **Plow Chat platform plugin for Hermes** — an agent's phone line. Inbound
arrives over a WebSocket the plugin dials out on; outbound and cron delivery go
back through the chat REST API.

```
plow-chat-platform/     exactly what gets installed, and nothing else
  plugin.yaml           the manifest -- registers the platform id
  __init__.py           the adapter; Hermes loads it from the plugin root
tests/                  the adapter suite
```

The directory is named for the plugin id so the install can be a directory copy:
`agent-mgr` snapshots this repo at the pinned SHA and swaps `plow-chat-platform/`
into place. Nothing else here — README, tests, justfile — reaches an agent.

> **Ordering.** That contract needs `agent-mgr`'s `install-plugin` to install
> this *directory*, which it does only from
> [`plow-pbc/agent-mgr#10`](https://github.com/plow-pbc/agent-mgr/pull/10) onward.
> Before that change it copied two files from the repository **root**, so a
> `runtime/plow-chat-plugin.ref` bumped to a SHA of this layout against an older
> `agent-mgr` installs an empty plugin directory — an agent with no phone line.
> Land the `agent-mgr` side first, then bump the pin.

## Who consumes this

[`plow-pbc/agent-mgr`](https://github.com/plow-pbc/agent-mgr) pins a SHA of this
repo in `runtime/plow-chat-plugin.ref` and installs it into every agent's home:

```sh
agent-mgr install-plugin <name>     # or as part of `agent-mgr restore <name>`
```

It lands as two files in the agent's own home, and nothing else:

```
~/.hermes-<name>/plugins/plow-chat-platform/
  __init__.py
  plugin.yaml
```

**Pinned by SHA, never vendored.** A branch ref would silently re-point a running
agent on the next push here, and this plugin holds the chat token. A vendored
copy stops receiving fixes — `sams-admin-hermes-agent` proved that, carrying a
v0.1.0 fork until it was archived.

## Configuration

Read from the agent's own dotenv (`$AGENT_HOME/.env`), never from the image or a
URL in git.

| var | required | meaning |
|---|---|---|
| `PLOW_CHAT_TOKEN` | yes | the session bearer token activation mints |
| `PLOW_CHAT_CHAT_UID` | yes | the chat this agent serves, `cht_…` |
| `PLOW_CHAT_BASE_URL` | no | API base, default `https://api.plow.co` |
| `PLOW_CHAT_GROUP_UIDS` | no | group chats to join, `<cht_id>=<display name>`, comma-separated |
| `PLOW_CHAT_HOME_CHANNEL` | no | delivery target for cron, defaults to `PLOW_CHAT_CHAT_UID` |
| `PLOW_CHAT_WELCOME_MESSAGE` / `PLOW_CHAT_AUTO_WELCOME` | no | one-time message on `chat_active` |
| `PLOW_CHAT_AUTO_APPROVE_PAIRING` | no | best-effort approval of verified Plow members |

`plugin.yaml` is the authority on this list; the table is a reader's summary.

One variable is **not** in `plugin.yaml`, because the operator does not set it —
the Hermes image does, and the adapter refuses to construct without it:

| var | set by | meaning |
|---|---|---|
| `HERMES_HOME` | the image (`/opt/data`) | state root. The adapter keeps `plow-chat-seen.json` there — the message uids it has dispatched per chat, which is what a reconnect asks before replaying. Resolved once at construction and fatal if unset, rather than defaulted to the working directory: a record read from the wrong root reads as a clean start, and a clean start replays nothing. |

## There is a second implementation, and it is not this one

`plow-pbc/plow`'s `cloud-agents/hermes/plugins/plow_chat/` registers the **same
plugin id** for Plow's multi-tenant cloud agents. Same wire path — `POST
/v1/ws/ticket` → `WSS /v1/ws?ticket=` → `POST /v1/chats/{uid}/messages` — but a
different env contract and a different feature set:

| | this repo | plow tenant |
|---|---|---|
| token / chat env | `PLOW_CHAT_TOKEN` / `PLOW_CHAT_CHAT_UID` | `PLOW_AGENT_TOKEN` / `PLOW_HOME_CHANNEL` |
| base env | `PLOW_CHAT_BASE_URL` | `PLOW_API_BASE`, **without** `/v1` |
| group chats | yes | none |
| persisted checkpoint, history backfill | **none** | yes |

So `plugins.enabled: [plow-chat-platform]` means different things depending on
which side installed it. **Convergence is the goal; this repo is not there yet.**
Tracked in [`#2`](https://github.com/plow-pbc/hermes-plow-chat/issues/2) (this
adapter drops inbound turns across a socket gap — the tenant's
`_anchor`/`_backfill` is the solved prior art) and
[`plow-pbc/plow#1394`](https://github.com/plow-pbc/plow/issues/1394). The first
was `plow-pbc/seed-hermes-plow#15`; it moved here because an archived repo's
issues are read-only.

A merge is constrained in one direction: the group path here calls
`GET /v1/chats`, which is user-wide, while a tenant deliberately holds a
chat-scoped token that cannot enumerate chats. Unifying means one adapter with
the reconcile path gated on token capability — not a file move.

`plow-pbc/plow`'s `cloud-agents/hermes/HERMES_INTEGRATION.md` is the best
reference for the underlying Hermes behaviour either implementation has to live
with — cron `deliver` semantics, the literal-token trap, why a request/response
shim cannot work.

## Development

```sh
just test
```

The suite loads `__init__.py` by path and stubs the `gateway.*` modules Hermes
supplies at runtime, so it needs no Hermes install and touches no network.

## Provenance

The history here is `plow-pbc/seed-hermes-plow`'s, carried over in full — this is
where the adapter was written, including the group layer ported onto it in
August 2026. That repo is **archived**: the SEED pattern it belonged to is
retired, and its remaining artifacts (`plow-connectors`,
`create_plow_chat_curl.sh`) stay frozen there at their pinned SHAs, which still
resolve because archived public repos remain readable.

What changed on the way over: the `ref/` layout is gone, and with it the ~30-line
root shim that existed only to bridge it. The adapter sits where Hermes loads it,
so an agent's installed plugin no longer carries a `ref/hermes-plugin/plow_chat/`
directory inside its home.
