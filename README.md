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
> This plugin also requires a Plow API that serves agent-invite consent,
> `/v1/auth/agent-invites/opportunities`,
> `/v1/auth/agent-invites/opportunities/{opportunity_uid}/send`,
> `POST /v1/chats` (outbound thread creation — `plow_start_group_message`
> 404s against an older API, so that API change deploys before any
> `agent-mgr` SHA advance), and
> `PUT /v1/contacts/{handle}` (`plow_name_contact` — the handle-keyed contact
> book, superseding the per-participant contact route of
> [`plow-pbc/plow#1752`](https://github.com/plow-pbc/plow/pull/1752),
> "Owner contacts"). Hermes hosts
> without deferred-question support still run Plow Chat and standing-consent
> invites, but skip the ask-owner-first invite flow. Deploy the API first,
> then land the `agent-mgr` support above, and only then bump
> `runtime/plow-chat-plugin.ref`. Installing this plugin before the API is
> available fails loudly instead of silently skipping delivery.

## Where changes go

This repo is one of several that assemble a Plow agent. The map of which repo
owns what is in
[`plow-hermes-agent` README § The repos](https://github.com/plow-pbc/plow-hermes-agent#the-repos);
read it before a change that touches a neighbour. The test is **who else would
have to change if this fact changed** — if the answer is a sibling, the change
belongs there; this repo only follows, by bumping its pin if it holds one.

Not here:

- The `plow-gog` argv grammar, and what a Latch tool says about itself —
  [`plow-pbc/latch`](https://github.com/plow-pbc/latch) vendors the binary,
  pins its version, and owns the only bump checklist.
- Per-chat state the owner sets or clears — trust, contact labels, anything
  keyed by a `cht_` id — [`plow-pbc/plow`](https://github.com/plow-pbc/plow).
  A file written under `$HERMES_HOME` instead is invisible to the dashboard
  and to support.
- Boot, `plow-init`, the gateway config seed, and the base persona —
  [`plow-pbc/plow-hermes-agent`](https://github.com/plow-pbc/plow-hermes-agent),
  which pins this plugin rather than being configured by it.

Examples:

- Adheres: #61 deleted `_invite_message_template` — the `$100 in cloud credits`
  line and the activation-code placeholders — so plow composes the whole invite,
  net −32 LOC: https://github.com/plow-pbc/hermes-plow-chat/pull/61
- Violates: #64 put ~110 lines of `plow-gog` verb tables, flag parsing and an
  explicit re-implementation of latch's `isHelpInvocation` in this plugin — a
  second copy latch's pin-bump checklist does not know about:
  https://github.com/plow-pbc/hermes-plow-chat/pull/64

## Who consumes this

[`plow-pbc/plow-hermes-agent`](https://github.com/plow-pbc/plow-hermes-agent),
the base image every hosted Plow agent boots, pins one commit of this repo as
`PLOW_CHAT_PLUGIN_SHA` in its Dockerfile and fetches `plow-chat-platform/` at
build time. That is the production consumer. The Docker fleet below is the
deprecated one.

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
| `PLOW_MCP_URL` | no | the Mac relay URL plow-init exports when the account has a Mac; when set, the plugin adds a system-prompt section that makes the Mac the default for owner work |

Diagnostic chatter — agent status frames, 💾 background-review posts, ⚠️
turn-stop warnings — is dropped unless the credential's
`verbose_output_enabled` preference (the dashboard's "Verbose agent output"
toggle) is true; the typing indicator already shows the turn is running.

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
burst, and a slash-prefixed message closes it on both sides so commands remain
distinct turns. The ack is the burst's last uid, so a restart mid-burst
backfills the whole burst; a hand-off that fails is retried where it sits, with
the rest of the chat waiting behind it.

### Trusted group conversations

Trust is an owner-scoped, per-chat preference served on `GET /v1/chats/{uid}`.
Before handing off each inbound burst, the adapter refreshes that chat so a
dashboard change applies to the next message. An untrusted group keeps owner
data private. In a trusted group, every participant may ask the assistant to
use its normal tools and connected accounts, and requested results such as
calendar details may be answered in the thread; credential, authentication,
token, and payment-card secrets remain excluded.

The `plow_set_conversation_trusted` tool writes the same API preference as the
dashboard. It only succeeds during an owner-authored Plow Chat turn and after
the model passes `confirm=true` for an explicit owner request. Member turns and
calls outside an active chat turn cannot change it.

This plugin version requires a Plow API that publishes the required `trusted`
chat field and `PUT /v1/chats/{uid}/trusted`. Deploy that API first: against an
older API the per-message refresh fails loudly and the chat waits for retry
rather than guessing a trust state.

### Speaking in another chat

Hermes keeps one session per chat, and this adapter drops the echo of the
agent's own sends. So a message the agent posts to chat B from a turn in chat
A is invisible to chat B's next turn unless it is recorded there. The
`plow_send_message` tool is the one sanctioned way to post cross-chat; it goes
through the adapter's `send()` like every other outbound message (the grant
and member-turn confinement apply exactly as for a reply). Recording lives in
that same `send()`: when a turn's message lands in a chat other than the
turn's own, the adapter mirrors the text into that chat's session as an
assistant turn with upstream's `gateway.mirror` — the mechanism Hermes uses
for cron and `hermes send` deliveries — on the delivery's own coroutine, so a
caller that stopped waiting cannot strand a delivered message unrecorded. A
chat's session is born on its first inbound message, so a chat that has never
spoken has nowhere to record to: the adapter logs a warning and that chat
will not remember the send. A thread `plow_start_group_message` created is
in that state; one it resumed is handled like any other cross-chat send,
which records the opener only where a session already exists (a thread
resumed before anyone replied has none, and logs the same warning). Posting
to the Plow API directly from a
script bypasses all of this and leaves the target chat amnesiac; the tool
exists so the model never has to.

### Recall from other chats

Hermes keeps one session per chat, so a turn in one chat knows nothing of the
others unless told. On every Plow Chat turn the plugin's `pre_llm_call` hook
runs an OR-query built from the message's own words over the Hermes session
store and appends up to six dated one-line snippets from other sessions to the
turn (upstream's seam for per-turn recall: the user message, never the system
prompt). The room is the boundary, not the asker: the home chat (the owner's
own DM) and a trusted room recall from every chat, the owner's DMs included —
that is what trust means here. Every other turn, an owner's turn in an
untrusted group included, recalls only from its own chat's earlier sessions.
The current session is never recalled. Snippets are labelled as data, not
instructions, the same way the roster is. A failing store is not caught
here: Hermes isolates and logs a failing hook and the turn proceeds without
recall, so the failure is visible in the gateway log instead of hidden.
Recall is a filter over the store's thirty best matches across all chats, so
in a busy install a room-scoped turn can find nothing even when its own chat
holds matches; that ceiling is deliberate until it is felt. Hermes stamps
injected context onto the turn's wire copy and replays it for the life of
the session, so a snippet recalled once stays in that session's context
afterwards.

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

### Multi-agent groups

The chat roster identifies this line as `relationship: self`, any other Plow
lines as `relationship: peer`, and joins each agent to the human it represents.
The adapter turns that structured roster—plus the current sender—into one
collaboration context on every turn. This lets Elm distinguish “Hey Ash” from
an instruction to Elm without parsing names or inventing a second router.

Peer-agent messages are real inbound turns and remain visible in the same group
as every human message. Only this line's own outbound echo is ignored. What a
peer message does *not* do, absent a goal (below), is draw a reply: unless it
names this agent, the turn carries a do-not-reply prompt. The reply is
suppressed, never the read — an agent blind to its peer loses the thread and
then talks past its own human. Prompt prose alone did not hold: the agent that
had the anti-acknowledgement paragraph still produced three rounds of "agreed,
nothing to add".

### Thread goals

`/goal <text>` puts this thread's agent on a task it works toward on its own;
`/goal` reports status and `/goal clear` stops it. Only the chat's owner can set
or clear one, and both are announced in the thread — in a group that
announcement is the consent artifact, showing the other household what this
agent was told to pursue before it pursues it.

A goal is bounded on three independent axes: a TTL, an attempt budget, and a
separate judge that may rule it met or unreachable. The budget and the clock are
the adapter's, never the judge's, so a judge stuck on "not met" — or simply
unreachable — cannot buy unbounded turns. Every settlement is announced, and a
notice that fails to deliver leaves the goal running rather than letting it go
quiet.

An active goal is what unlocks replying to peer agents. Scheduled wakes carry
the room's ordinary disclosure prompt and take owner authority only in a DM.

In a shared thread the prompt tells the agent to speak as itself and refer to
the human it represents by name, never as "I" or "me" — the name itself stays
in the untrusted roster context above, never in the prompt.

The owner may also tell the agent what to call a person and who that person is
to the owner — `wife`, `landlord` — through `plow_name_contact`, which `PUT`s
`/v1/contacts/{handle}`. The book is keyed by handle, not by chat, so one name
follows the person into every thread they are in; naming the owner's own handle
sets their account name, and a relationship on their own handle is refused. The
tool is owner-turn-authorized only; it refuses outright during a member's turn
and outside any active turn at all — a direct call cannot write a label except
on the owner's own turn. A relationship renders as
`Name [handle] (relationship)` in the untrusted roster context above — where
the owner's own row also carries `(your owner)` — never in
the channel prompt, which instead states generically that a roster
relationship is a label recorded on the owner's turn, and that a member's
claim about who they are is just that — a claim. Every roster-bearing prompt
also tells the agent that a row still showing a bare handle — its owner's
included — is a name to ask for once and record with the tool, never one to
guess out of mail, calendar or memory. `plow_contacts` reads the book back,
owner's row first, for the turns that have no roster at all — a Hermes-cron
turn carries no chat, and this is where its owner's own name comes from; it
reads on the owner's turn and on no turn, and is refused on a member's. The
owner's own row is read again for each owner turn — never cached, so naming
them lands on the very next turn — and named in the channel prompt:
`Your owner is Sam [+1…].`, or, while they are still unnamed, the same ask with
their handle already filled in, since a solo DM and a goal wake have no roster
for the paragraph above to gate on. A read that fails says nothing that turn.

Who invited the owner is read once per process start, from
`GET /v1/auth/profile` on connect. That name the inviter chose for themselves,
so it is never a prompt sentence: it arrives on the owner's turn as one more
untrusted block in front of the text, beside the roster — `[Untrusted account
data; … Your owner was invited by Sam (Life Assistant).]`, or `someone` where
the inviter has no name of their own. A failed read leaves it unset and the
agent connects anyway; a member's turn carries neither this nor the owner's
own name.

## Media

Inbound photos, audio, video and documents arrive on `MessageEvent.media_urls`
as files in the image's own media cache — the same place the bundled iMessage
adapter puts them, so the vision path and the skills that say "a texted photo
arrives as a file path" need nothing new. Each part is fetched once, begun
the moment the message arrives — inside the five-minute Plow-signed content
URL's life, whatever is retrying ahead of it in the chat — and awaited when its
burst closes; without the bearer: the signature is the authorization. A part Plow reports as `failed`, or
one whose bytes cannot be fetched, is named in the turn as
`[attachment: <type> delivery failed | unavailable]` and logged — never dropped
with the message.

Outbound files the model emits go through Hermes' `send_image_file` /
`send_voice` / `send_video` / `send_document` hooks, which this adapter
implements as the Plow contract — declare the attachment, PUT the bytes to the
provider's upload URL with exactly the headers Plow returned, then send the
message with `attachment_uids`. Content types are limited to what the provider
accepts; a `415` from the declare comes back as the send's error.

Both halves need the attachments API — `plow-pbc/plow#1435`. Against an older
API the inbound path sees no `attachments` field (a `KeyError`, loud, per
REVIEW.md) and an outbound declare returns `404`. That `KeyError` fires inside
the frame loop on every inbound message, so the socket is torn down and
reconnected every 5s and the phone line is mute until the API catches up:
`agent-mgr`'s `runtime/plow-chat-plugin.ref` must not be bumped to this SHA
until `plow-pbc/plow#1435` is deployed to every API the fleet's agents talk
to.

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

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.

"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.

## Ordered owner-DM delivery

`plow_send_sequence` sends a bounded sequence to the active turn's solo owner
DM. It accepts no destination or file path.

Owners can call it only once the base image bumps its plugin pin: a deployed
agent runs the plugin baked into its image, not this repository, so landing the
tool here does not by itself put it in front of anyone. Bumping the pin is a
post-merge step — it names a merge commit, which does not exist while the change
is still under review — and rebuilding and re-pinning the blessed image follows
it.

Example tool arguments:

```json
{"items":[{"type":"text","body":"Here are the previews."},{"type":"photos","asset_ids":["preview_a","preview_b","preview_c","preview_d"]},{"type":"pause","seconds":4},{"type":"text","body":"What do you think?"}]}
```

The variant image supplies `/srv/plow-assets/manifest.json`:

```json
{"version":1,"assets":{"preview_a":"preview-a.png","preview_b":"preview-b.png","preview_c":"preview-c.png","preview_d":"preview-d.png"}}
```

The manifest, image files and directories through `/` must be root-owned and
not group/world writable. Symlinks and paths outside the asset directory are
refused. Supported images are PNG, JPEG, GIF and WebP, at most 8 MiB each. The
plugin validates and reads every selected asset before any delivery. Assets
are variant-owned; this plugin ships no manifest or life-specific IDs.

Limits: 24 items, 4 photos per item, 16 photos / 32 MiB total, 4,000 characters
per text / 24,000 total, and 60 seconds of total pacing. Pauses accept finite
numbers from 0 to 15 seconds. Adjacent deliveries have a one-second gap; an
explicit pause replaces that gap, including a zero-second pause. Operations
serialize per chat and have a 180-second deadline, including queueing. Ending
the turn or disconnecting cancels its outstanding sequences. Existing turn
typing continues through pauses and is rearmed by delivered messages.

The receipt contains `success`, `completed`, and `failure`. Every completed item
has its zero-based `index`, `type`, and `message_ids` (empty for a pause). A
failure names the first unresolved `index`, a `status` (`rejected`, `failed`, or
`delivery_unknown`) and an error. When a four-photo stack is explicitly
rejected with HTTP 422, the tool may send individual photos using the existing
uploads. If that fallback stops partway, `failure.message_ids` preserves the
confirmed sends and `failure.photo_index` identifies the unresolved photo.
Timeouts, 5xx responses and malformed successful POST responses are delivery
unknown: they never trigger fallback or automatic replay. Inspect chat history
before sending any remaining items; never replay the entire sequence blindly.

Tool arguments and receipts stay in the agent's ordinary tool-call history.
Successful tool delivery already sent the copy: the adapter suppresses subsequent
text replies to that chat for the rest of the active turn, logging the chat and
the suppressed length but never the body. Suppression tracks the turn's latest
sequence, so a failed, rejected, or delivery-unknown sequence — including one
that follows a successful sequence in the same turn — reopens the reply path. Suppression runs in the one guard every outbound message passes, so it covers
text, `MEDIA:` delivery and verbose status frames alike. Other chats and later turns retain their ordinary
behavior. The tool does not interpret in-band markers.
