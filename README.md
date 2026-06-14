# seed-hermes-plow — Plow on Hermes

## Purpose

This seed runs **Plow on Hermes**. It bundles two capabilities for a
Docker-backed Hermes Agent:

1. the **`plow_chat` messaging gateway** — REST + WSS to the Plow Chat API, so
   the owner messages Hermes over their Plow line (specified below); and
2. the **`plow-connectors` skill** — lets the agent use the owner's
   Plow-connected **Gmail, Google Calendar, and Slack** accounts with the same
   credentials, no separate setup (see *Plow connectors* near the end).

It is the superset "Plow capabilities on Hermes" seed. The rest of this document
specifies the messaging gateway.

This SEED provides the Hermes gateway platform plugin named `plow_chat`. It
wires Hermes to the [Plow Chat](https://github.com/plow-pbc/seed-plow-chat)
API: Hermes sends replies through `POST /v1/chats/{chat_uid}/messages` and
receives user replies from Plow's WebSocket stream.

The target install model is direct mount into a seed-hermes Docker scaffold.
The host places this repository's plugin files under
`./hermes-agent/data/plugins/plow-chat-platform/`, pre-enables the manifest name
in `./hermes-agent/data/config.yaml`, writes `PLOW_CHAT_*` to
`./hermes-agent/data/.env`, and then starts the Hermes container. The host does
not run `hermes plugins install`, clone git repositories, or depend on Python.

## Required plugin files

The mounted plugin directory must contain this exact file set:

```text
hermes-agent/data/plugins/plow-chat-platform/
  plugin.yaml
  __init__.py
  ref/hermes-plugin/plow_chat/adapter.py
```

`plugin.yaml` is the manifest Hermes discovers. `__init__.py` loads
`ref/hermes-plugin/plow_chat/adapter.py` and raises `ImportError` during boot if
the adapter is missing, so preserving that layout is required.

## Direct-mount install

From the parent folder that contains the seed-hermes scaffold at
`./hermes-agent/`:

```bash
curl -fsSL https://raw.githubusercontent.com/plow-pbc/seed-hermes-plow-chat/main/ref/scripts/install_direct_mount.sh \
  -o /tmp/install_plow_chat.sh
bash /tmp/install_plow_chat.sh --scaffold ./hermes-agent
```

Pin a published source with `PLOW_CHAT_PLUGIN_REF=<branch-or-sha>`. When running
from a local checkout, avoid network fetches by copying from the checkout:

```bash
PLOW_CHAT_PLUGIN_LOCAL_DIR=. ref/scripts/install_direct_mount.sh --scaffold ./hermes-agent
```

The resulting `hermes-agent/data/config.yaml` must include the manifest name:

```yaml
plugins:
  enabled:
    - plow-chat-platform
  disabled: []
terminal:
  cwd: /opt/data/workspace
```

## Create and verify a Plow chat

Use the curl-only host helper to start Plow activation with `provision_chat=true`,
capture the returned session token and chat uid after verification, and write
`PLOW_CHAT_*` to the target profile's `.env` before first container boot:

```bash
# Default profile (writes hermes-agent/data/.env):
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent
```

### Per-profile activation

A multi-profile install (e.g. an owner profile `daniel` plus a team-listener
profile `daniel-team`) keeps each profile's credentials in its own env file
under `data/profiles/<name>/.env`. Pass `--profile <name>` and the helper
resolves the target automatically:

```bash
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel-team
```

`--profile <name>` is equivalent to `--data-dir ./hermes-agent/data/profiles/<name>`;
use `--data-dir` directly when the profile lives outside the scaffold's
`data/profiles/` tree. Run the helper once per profile before first boot.

The script asks Plow to assign an available line and prints the instruction the
user needs: `Text Plow Activate: ABCDE from iMessage to +1...`. It does not
print the session token. It also writes a redacted `.activation.json` audit
file (in the same profile dir) with the activation secret removed and only the
token last four retained. Start Hermes with `docker compose up` after the
script writes `.env`, so the container boots once with the Plow Chat platform
enabled.

### Confirming activation succeeded

On success the helper prints a verification line naming the profile and the
exact env file it wrote, so you can confirm Phase 4 worked without opening the
file by hand:

```text
Verified: chat is active.
Chat uid: cht_...
Profile daniel activated. Wrote PLOW_CHAT_CHAT_UID + PLOW_CHAT_TOKEN to ./hermes-agent/data/profiles/daniel/.env.
Wrote redacted activation audit to ./hermes-agent/data/profiles/daniel/.activation.json
```

The resulting profile `.env` contains these values (mode `600`):

```bash
PLOW_CHAT_BASE_URL=https://api.plow.co
PLOW_CHAT_CHAT_UID=cht_<opaque-chat-id>      # the provisioned Plow chat uid
PLOW_CHAT_TOKEN=<opaque-bearer-token>        # user Bearer credential — never commit/log
PLOW_CHAT_HOME_CHANNEL=cht_<opaque-chat-id>  # same value as PLOW_CHAT_CHAT_UID
```

### If the activation code expires

The displayed code is single-use and time-limited. If it expires before the
text arrives, Plow's redeem endpoint returns HTTP 410; the helper detects this,
prints `Activation code expired.` plus the exact command to re-run for a fresh
code, and exits non-zero (75) instead of surfacing a raw `curl: (22)` error.
Just run the same command again.

### Non-interactive test mode (testing/CI only)

Phase 4 normally requires a human texting the activation code from the target
iPhone, which cannot complete in a headless DinD/CI environment. For test
validation only, `--test-mode` skips the phone-bind dance and writes
operator-supplied credentials straight to the profile `.env`:

```bash
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel \
  --test-mode --test-chat-uid cht_known --test-token tok_known
```

This is **not** a real activation — it never contacts Plow and the audit file
records `"status": "test-mode"`. Never use it for a real operator install.

The host poll uses:

```bash
curl -sSL \
  -H 'Content-Type: application/json' \
  -d '{"activation_secret":"<secret>"}' \
  "https://api.plow.co/v1/auth/activate/redeem"
```

If the adapter is connected when Plow emits `chat_active`, it sends exactly one
welcome message from Hermes through the normal Plow message endpoint. Set
`PLOW_CHAT_WELCOME_MESSAGE` to customize it or
`PLOW_CHAT_AUTO_WELCOME=false` to disable it.

## Runtime behavior

- `PLOW_CHAT_CHAT_UID` is the single Plow chat handled by this plugin instance.
- `PLOW_CHAT_TOKEN` stays in the profile's `.env` (scaffold `data/.env`, or
  `data/profiles/<name>/.env` for a named profile); do not commit it or log it.
- The activation Bearer token is a user credential, not just a chat secret.
  Keep the profile `.env` and `.activation.json` mode `600`.
- The adapter sends the welcome on the first `chat_active` frame it sees.
- Inbound WebSocket frames with `direction=outbound` are ignored so Hermes does
  not answer itself.
- The adapter best-effort approves verified Plow member ids in Hermes'
  `plow_chat` pairing store so the first inbound message reaches Hermes.
- Rich Markdown is flattened to plain text because the backing channel is
  iMessage/SMS-style.

## Plow connectors (Gmail · Google Calendar · Slack)

Beyond messaging, this seed can give Hermes the owner's Plow-connected Google
(Gmail + Google Calendar) and Slack accounts. The `plow-connectors` skill wraps
the Plow connector REST API (`POST /v1/connectors/<connector>/<action>`, plus
`GET .../status`) and reuses the gateway's existing `PLOW_CHAT_TOKEN` /
`PLOW_CHAT_BASE_URL`, so there are no new credentials to manage.

Install it into the scaffold (this does not start the container):

```bash
ref/scripts/install_connectors.sh --scaffold ./hermes-agent
```

It copies the skill to `data/skills/plow-connectors/`. After the next
`docker compose up`, the agent uses it via its shell, e.g.:

```bash
python3 /opt/data/skills/plow-connectors/plow_connector.py gmail status
python3 /opt/data/skills/plow-connectors/plow_connector.py gmail messages.list '{"query":"is:unread","max_results":5}'
python3 /opt/data/skills/plow-connectors/plow_connector.py slack channels.list '{"account":"T0..."}'
```

If Google/Slack are connected to a **different** Plow account than the one that
owns the chat line, point the connectors at it with `PLOW_CONNECTOR_TOKEN` in
the resolved profile `.env` (`data/.env` for the default profile,
`data/profiles/<name>/.env` for a named profile); it overrides `PLOW_CHAT_TOKEN`
for connector calls only, leaving messaging on the chat account. A connector
whose `status` reports `connected:false` simply is not
linked to that account yet (linking is a one-time OAuth consent in Plow, out of
scope for this seed).

## License

MIT.
