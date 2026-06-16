# Purpose

> See [README#Purpose](README.md#purpose).

## Normative Language

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described in RFC 2119.

`Implementation-defined` means the behavior is part of the implementation contract; this specification does not prescribe a single policy.

Sub-folder SEEDs in this tree inherit the RFC 2119 declaration. They MUST NOT re-declare it.

## Dependencies

### API

- The Plow Chat API surface — endpoints, headers, WebSocket frame types, auth model, chat creation, and verification semantics — is defined by the external SEED at `https://github.com/plow-pbc/seed-plow-chat`. A consumer MUST read that SEED first; this SEED depends on its `## Objects` and `## Actions` and does not restate them.

### Runtime

- Hermes Agent MUST run in the Docker-backed `seed-hermes` shape: a host `compose.yaml`, whole `./data:/opt/data` mount, and `HERMES_HOME=/opt/data` inside the container.
- Hermes' container runtime MUST have gateway/plugin support and Python `aiohttp`; the official Hermes image supplies the runtime dependencies for this plugin.
- The host setup path MUST NOT require host `hermes`, host Python, git, `hermes plugins install`, host GitHub token environment variables, or container-side network/plugin installation.
- The host setup path MAY use `curl` and standard shell tools. `gh` MAY be used by a higher-level seed as an accelerator, but it is not required by this SEED.

## Objects

The named entities that exist on the Hermes side. For Plow Chat entities (chats, lines, members, messages, WebSocket frames), see the [Plow Chat Objects](https://github.com/plow-pbc/seed-plow-chat/blob/main/SEED.md#objects).

### plow_chat Hermes platform

- A Hermes gateway platform named `plow_chat`.
- It maps a Plow chat to a Hermes session source with `platform='plow_chat'`, `chat_id=<chat.uid>`, `chat_type='dm'`, and member sender metadata as the user identity.
- It sends plain text responses through Plow's message creation endpoint.
- It receives user messages by minting short-lived WebSocket tickets and subscribing to `wss://api.plow.co/v1/ws?ticket=<ticket>`.

### Direct-mounted plugin

- The plugin MUST be placed on the host at `<seed-hermes-scaffold>/data/plugins/plow-chat-platform/`, which the Hermes container sees as `/opt/data/plugins/plow-chat-platform/`.
- The mounted plugin directory MUST include `plugin.yaml`, `__init__.py`, and `ref/hermes-plugin/plow_chat/adapter.py`, preserving that layout.
- The root `__init__.py` MUST load and re-export `register(ctx)` from `ref/hermes-plugin/plow_chat/adapter.py`; if that adapter file is missing, it MUST raise `ImportError` at boot.
- Hermes config MUST enable the manifest name `plow-chat-platform` in `plugins.enabled`; the registered platform name remains `plow_chat`.

### Host orchestration scripts

- `ref/scripts/install_direct_mount.sh` is the canonical curl/shell installer for this gateway. It targets a seed-hermes scaffold with `--scaffold <dir>` (default `./hermes-agent`) or an explicit `--data-dir <dir>`, places the required plugin file set under `data/plugins/plow-chat-platform/`, enables `plow-chat-platform` in `data/config.yaml`, and supports `PLOW_CHAT_PLUGIN_LOCAL_DIR` and `PLOW_CHAT_PLUGIN_REF` source overrides.
- `ref/scripts/create_plow_chat_curl.sh` is a curl/shell helper that targets the same scaffold with `--scaffold <dir>`, `--profile <name>`, or `--data-dir <dir>`, starts Plow activation with `provision_chat=true`, prints the one-time activation message, writes `PLOW_CHAT_*` to the target profile's `.env`, and polls activation redeem until verified or timeout.
- The helper MUST resolve its target data dir as follows, highest precedence first: an explicit `--data-dir <dir>`; else `--profile <name>` (or `PLOW_CHAT_PROFILE`) resolving to `<scaffold>/data/profiles/<name>`; else the scaffold's `data/`. This MUST match the per-profile `.env` files that downstream install verification reads.

### plow-connectors Hermes skill

- An OPTIONAL Hermes skill named `plow-connectors` that lets the agent use the owner's Plow-connected Google (Gmail + Google Calendar) and Slack accounts. A scaffold MAY install only the `plow_chat` gateway and no connectors.
- It is a single generic wrapper (`plow_connector.py`, Python stdlib only) over the Plow connector REST API: `GET /v1/connectors/<connector>/status` and `POST /v1/connectors/<connector>/<action>` with a JSON body, for `<connector>` in {`gmail`, `slack`}. Google Calendar actions (`calendar.events.*`, `calendar.list`, `calendar.freebusy`) live under the `gmail` connector.
- It authenticates with the same user Bearer token the gateway already holds: it reads `PLOW_CONNECTOR_TOKEN` else `PLOW_CHAT_TOKEN`, against `PLOW_CHAT_BASE_URL` (default `https://api.plow.co`). It MUST NOT define new credentials.
- `ref/scripts/install_connectors.sh` is the curl/shell installer for the skill, targeting a seed-hermes scaffold with `--scaffold <dir>` (default `./hermes-agent`) or `--data-dir <dir>`.

## Actions

The verbs performed by the Hermes-side objects. For Plow Chat actions (chat is created, chat is verified, message is sent, WebSocket subscription is opened, message is received), see the [Plow Chat Actions](https://github.com/plow-pbc/seed-plow-chat/blob/main/SEED.md#actions).

### plow_chat is installed by direct mount

- A host agent MUST run `ref/scripts/install_direct_mount.sh --scaffold <seed-hermes-scaffold>` to place the plugin files directly into `data/plugins/plow-chat-platform/`; it MUST NOT call `hermes plugins install`, `hermes plugins enable`, `git clone`, or any Python installer on the host.
- A host agent MUST ensure `<seed-hermes-scaffold>/data/config.yaml` exists before first `docker compose up` and contains `plugins.enabled` with `plow-chat-platform`.
- A host agent SHOULD preserve any existing `config.yaml` values while adding this plugin. If no config exists, it MAY write the minimal plugin/terminal skeleton shown in the README.
- A host agent MAY set `PLOW_CHAT_PLUGIN_LOCAL_DIR` to copy plugin files from a local checkout, or `PLOW_CHAT_PLUGIN_REF` to fetch a specific branch/SHA from GitHub raw URLs.
- The direct-mount installer MUST NOT start the Hermes container. The host sequence is: install the plugin files, run `create_plow_chat_curl.sh` until verified, then start `docker compose up` once with `data/.env` populated.

### plow_chat is configured with Plow chat credentials

- A host agent MUST run `ref/scripts/create_plow_chat_curl.sh --scaffold <seed-hermes-scaffold>` (optionally with `--profile <name>`) before first Hermes boot for this gateway and write these values into the resolved profile `.env` (`data/.env`, or `data/profiles/<name>/.env`): `PLOW_CHAT_BASE_URL`, `PLOW_CHAT_CHAT_UID`, `PLOW_CHAT_TOKEN`, and `PLOW_CHAT_HOME_CHANNEL`.
- On successful verification the helper MUST print a confirmation that names the profile and the exact env file written, e.g. `Profile <name> activated. Wrote PLOW_CHAT_CHAT_UID + PLOW_CHAT_TOKEN to <path>.`, so an operator can confirm Phase 4 succeeded without inspecting the env file.
- Before writing, the helper MUST (re-)ensure the target data dir is writable, because seed-hermes prepare.sh and the running container can churn ownership/mode on the bind-mounted `data/` tree. If the env file cannot be written, the helper MUST exit non-zero with an actionable error (the unwritable path plus a remediation command); it MUST NOT silently skip the write.
- The session token MUST be configured through `PLOW_CHAT_TOKEN`; it MUST NOT be committed, printed in logs, or placed in `config.yaml`. The shortcut activation returns a user-wide Bearer token that can read user identity surfaces such as `/v1/auth/owner-identity` and `/v1/me/channels`; treat it as a user credential, not a per-chat secret. `data/.env` MUST be mode `600` where the host filesystem supports it.
- When `PLOW_CHAT_CHAT_UID` and `PLOW_CHAT_TOKEN` are present, the plugin's `env_enablement_fn` SHOULD auto-enable `gateway.platforms.plow_chat` and set its home channel to the Plow chat uid.

### Host creates and polls Plow chat with curl

- The host flow MUST call `POST /v1/auth/activate` with `provision_chat=true`, capture `activation_secret`, and surface `Plow Activate: <display_code>` plus `send_to` to the human.
- The host flow MUST poll `POST /v1/auth/activate/redeem` with `{"activation_secret":"..."}` until redeem returns `status:"verified"` or a local timeout expires. The poll MUST capture the HTTP status without aborting on non-2xx, so an expired or gone activation does not surface as a raw `curl` transport error.
- When redeem returns HTTP 410, the helper MUST print a human-readable explanation and exit non-zero, never surfacing only the raw `curl: (22) ... 410` error. It MUST distinguish two cases by how soon the 410 arrives: an **immediate** 410 means the server returned an already-expired/deduped activation rather than minting a fresh one, so the helper MUST warn that re-running now returns the SAME stale code and advise waiting several minutes for the line's activation to clear before retrying; a **genuine later** 410 (the code expired after the human had time to text it) MUST print the exact command to re-run for a fresh code.
- The host flow MUST write the verified redeem `token` and embedded `chat.uid` into the scaffold env as `PLOW_CHAT_TOKEN` and `PLOW_CHAT_CHAT_UID`.
- The host flow MUST write `data/.activation.json` with mode `600` for audit: activation metadata with `activation_secret` redacted, token last four only, chat uid, line/send-to metadata, and owner/channel snapshots fetched with the verified Bearer token.
- The timeout path MUST tell the operator that the activation may not have arrived or the code may have expired, and SHOULD print the exact command to re-run; recovery is to start activation again.
- Phase 4 verification depends on an external human texting the activation code from the target iPhone, which cannot complete in a headless DinD/CI environment. The helper MAY provide a `--test-mode` that, given operator-supplied `--test-chat-uid`/`--test-token` (or `PLOW_CHAT_TEST_CHAT_UID`/`PLOW_CHAT_TEST_TOKEN`), skips the phone-bind dance and writes those values to the profile `.env`. Test mode MUST NOT contact Plow, MUST record `status:"test-mode"` in the audit file, and is for test validation only — not for real operator activation.

### plow_chat sends a Hermes response

- The platform adapter implements the Plow Chat [Message is sent](https://github.com/plow-pbc/seed-plow-chat/blob/main/SEED.md#message-is-sent) action against the configured chat.
- The adapter SHOULD strip or flatten rich markdown because the current backing channel is iMessage/SMS-style chat.
- The adapter MUST treat 409 `chat_not_ready` as a setup/verification problem, not a successful send.

### plow_chat receives a user message

- The adapter implements the Plow Chat [WebSocket subscription is opened](https://github.com/plow-pbc/seed-plow-chat/blob/main/SEED.md#websocket-subscription-is-opened) and [Message is received](https://github.com/plow-pbc/seed-plow-chat/blob/main/SEED.md#message-is-received) actions.
- For inbound frames, the adapter constructs a Hermes `MessageEvent` with the message body, Plow message uid, chat uid, and member sender uid/name.
- Before dispatching an inbound member message into Hermes, the adapter SHOULD best-effort approve that member uid in Hermes' `plow_chat` pairing store so the verified Plow participant does not hit a second generic DM pairing flow.
- On disconnect, the adapter SHOULD reconnect by minting a fresh ticket and SHOULD backfill missed messages with `GET /v1/chats/{chat_uid}/messages`.

### plow_chat handles status and activation frames

- The adapter MAY log `message_status_updated` frames for outbound delivery transitions.
- If Plow emits `chat_active` while the adapter is connected, the adapter SHOULD send exactly one setup-success welcome message through the normal Plow message endpoint.
- The adapter SHOULD surface `chat_activation_failed` as a fatal setup error, because recovery is delete and recreate.
- The adapter SHOULD mark the platform connected only after the WebSocket's initial `connected` frame.

### plow-connectors is installed and used

- A host agent MAY run `ref/scripts/install_connectors.sh --scaffold <seed-hermes-scaffold>` to copy the skill file set into `data/skills/plow-connectors/`. Like the direct-mount installer, it MUST NOT call `hermes`, `git`, or a Python installer, and MUST NOT start the container.
- When the connectors are linked to a different Plow account than the chat line, the operator MAY set `PLOW_CONNECTOR_TOKEN` in the resolved profile `.env` (`data/.env`, or `data/profiles/<name>/.env`); it overrides `PLOW_CHAT_TOKEN` for connector calls only, leaving messaging on the chat account. The connector token is a user credential: mode `600`, never committed or logged.
- The helper MUST issue `status` as a GET and every other action as a POST whose JSON body is the request. It MUST reject an `action` that is not a single connector action token (no `/`, `?`, or `..`), so a prompted agent cannot reach arbitrary bearer-authenticated Plow API paths. A non-2xx response MUST be fatal (non-zero exit, error body to stderr); the helper MUST NOT swallow it.
- A connector whose `status` reports `connected:false` is not linked to that account; linking is a one-time OAuth consent in Plow and is out of scope for this seed.

## Verify

1. **Direct file-set check.** Run `PLOW_CHAT_PLUGIN_LOCAL_DIR=. ref/scripts/install_direct_mount.sh --scaffold "$(mktemp -d)/hermes-agent"`. Does it create `data/plugins/plow-chat-platform/plugin.yaml`, `data/plugins/plow-chat-platform/__init__.py`, and `data/plugins/plow-chat-platform/ref/hermes-plugin/plow_chat/adapter.py`? Expected: yes.

2. **Config enablement check.** Inspect the generated `config.yaml`. Does `plugins.enabled` include `plow-chat-platform` before first boot? Expected: yes.

3. **Hermes adapter-shape check.** Inspect `ref/hermes-plugin/plow_chat/adapter.py`. Does it define a `PlowChatAdapter` implementing `connect`, `disconnect`, and `send`, and a `register(ctx)` function that registers platform name `plow_chat`? Expected: yes.

4. **Host shell syntax check.** Run `bash -n ref/scripts/install_direct_mount.sh ref/scripts/create_plow_chat_curl.sh`. Does it exit 0? Expected: yes.

5. **Secret hygiene check.** Search committed files. Do they avoid literal-looking session tokens and one-time activation codes? Expected: yes.

6. **Container plugin-load check.** Prepare `./data` with the direct file set, `plugins.enabled: [plow-chat-platform]`, and dummy or real `PLOW_CHAT_*`; run `docker compose up`. Do logs show platform `plow_chat` registered and no `ImportError` from the plugin root? Expected: yes.

7. **Optional live chat check.** Run `ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent`, text the printed activation message from iMessage to the printed phone number, and let the script poll before starting Hermes. Does it report `verified`, write `PLOW_CHAT_TOKEN`, `PLOW_CHAT_CHAT_UID`, and a redacted `data/.activation.json`, and does a normal iMessage reply reach Hermes after first container start? Expected: yes.

8. **Connector skill check.** Run `python3 -m py_compile ref/hermes-skill/plow-connectors/plow_connector.py` and `ref/scripts/install_connectors.sh --scaffold "$(mktemp -d)/hermes-agent"`. Does the helper compile and the skill install under `data/skills/plow-connectors/` (with `SKILL.md` and an executable `plow_connector.py`)? Expected: yes.

## Open

- The sample plugin does not implement media attachments, streaming draft edits, delete-message, or native button UI for clarify prompts.
- The sample plugin does not yet include production-grade reconnect backfill cursor persistence; it documents the expected behavior and includes a simple reconnect loop.
- The Plow Chat API is pre-1.0 and may change without backwards compatibility guarantees; see the [seed-plow-chat Open items](https://github.com/plow-pbc/seed-plow-chat/blob/main/SEED.md#open).

## Non-Goals

- This SEED does not document the Plow Chat API; see the [seed-plow-chat SEED](https://github.com/plow-pbc/seed-plow-chat).
- This SEED does not store or publish session tokens, activation codes, phone numbers, or provider identities in committed files.
- This SEED does not require modifying Hermes core; a plugin-based adapter is the preferred shape when supported.
- This SEED does not install or enable Hermes plugins through the Hermes CLI on the target path.
