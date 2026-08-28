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
| `PLOW_AGENT_TOKEN` | yes | the chat-scoped bearer activation mints |
| `PLOW_HOME_CHANNEL` | yes | the home chat, `cht_…` — where cron and default output land. Must be inside the credential's grant; a grant without it refuses to connect |
| `PLOW_API_BASE` | no | API base, default `https://api.plow.co` (no `/v1` suffix) |

`plugin.yaml` is the authority on this list; the table is a reader's summary.

**Which chats the agent serves is not configured here at all.** The credential's
grant (`sessions.chat_uids`, served by `GET /v1/chats`) decides, refreshed on
every reconnect. Per-chat checkpoints persist under the agent home, and a
reconnect backfills each granted chat from its checkpoint, so a socket gap
drops nothing.

One person's rapid-fire messages are one turn: inbound is buffered per chat
for a 2s window that resets on each arrival — iMessage's bubble + link-preview
split, or a thought sent as two lines, reaches hermes as a single message
instead of the second interrupting the first. A change of speaker closes the
burst, so a group's order holds. The ack is the burst's last uid, so a restart
mid-burst backfills the whole burst — and so does a hand-off that fails.

### What a group thread is called

The home chat is always `Plow Chat`. Every other granted thread is named from
its own iMessage title — `<title> (<cht_ id>)`, the uid suffix making a title
structurally unable to take another room's name — or by its bare `cht_` id
when nobody has titled it. Titling the thread in iMessage is how it gets a
name; there is no name configuration here.

The result is published into the image's `channel_aliases.json` overlay on
every (re)connect, which is re-applied on every channel-directory build and
load, so a granted thread is addressable — and visible to `send_message
action="list"` — before it has ever spoken. The suffix does not get in the way
of addressing: the image's resolver falls back to an unambiguous prefix match,
so `plow_chat:#Snoqualmie Cabin Cleaning` reaches
`Snoqualmie Cabin Cleaning (cht_...)`. A name grants nothing — reach and
authority stay with the credential's grant. A retitle mid-connection shows up
on the next reconnect.

## One implementation, two delivery paths

This adapter is the only plow_chat implementation. `agent-mgr` installs it into
Docker-fleet agents at the SHA pinned in its `runtime/plow-chat-plugin.ref`;
`plow-pbc/plow`'s blessed exe.dev image bakes the same tree at the same pin.
The old second implementation in `plow`'s `cloud-agents/` was retired by the
unification (`plow-pbc/plow#1420`); its multi-chat credential-scope design is
what this adapter now is.

`plow-pbc/plow`'s `cloud-agents/hermes/HERMES_INTEGRATION.md` remains the best
reference for the underlying Hermes behaviour.

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
