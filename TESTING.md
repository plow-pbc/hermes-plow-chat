# Testing

## Maintainer checks

```bash
just test
```

Equivalent direct commands:

```bash
ref/verify.sh
uvx pytest -q
```

These checks prove the direct-mounted file set is complete, `config.yaml`
enables `plow-chat-platform`, host helper scripts are syntax-valid, the adapter
shape is intact, and committed files do not contain literal Plow secrets or
real activation codes.

## Container plugin-load check

From a disposable Hermes host folder:

```bash
mkdir -p hermes-agent/data
PLOW_CHAT_PLUGIN_LOCAL_DIR=/path/to/seed-hermes-plow \
  ref/scripts/install_direct_mount.sh --scaffold ./hermes-agent
cat >> hermes-agent/data/.env <<'EOF'
PLOW_CHAT_BASE_URL=https://api.plow.co
PLOW_CHAT_CHAT_UID=cht_dummy_for_load_check
PLOW_CHAT_TOKEN=dummy_for_load_check
PLOW_CHAT_HOME_CHANNEL=cht_dummy_for_load_check
EOF
docker compose up
```

Expected evidence:

- Hermes discovers and enables manifest `plow-chat-platform`.
- Gateway logs show platform `plow_chat` registered.
- No `ImportError` references `ref/hermes-plugin/plow_chat/adapter.py`.

The dummy token is only for import/load verification. The WebSocket ticket
request may fail later because the chat credentials are not real.

## Live Plow verification run

```bash
mkdir -p hermes-agent/data
PLOW_CHAT_PLUGIN_LOCAL_DIR=. ref/scripts/install_direct_mount.sh --scaffold ./hermes-agent
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent
```

The script asks Plow to assign a line. Use `--base-url` only if you are
targeting a non-production Plow API.

Text the printed `Plow Activate: ABCDE` message from iMessage to the printed
Plow line. The host script should report:

```text
Status: pending
Status: verified
Verified: chat is active.
```

The helper should write `hermes-agent/data/.env` and a redacted
`hermes-agent/data/.activation.json`. Then start Hermes:

```bash
docker compose up
```

Expected Hermes evidence:

- A normal reply from iMessage is delivered to Hermes as a `plow_chat` inbound
  message.

If the poll times out, start activation again. Common causes are the code
expiring, texting from the wrong messaging identity, or texting the wrong line.
If the code expires mid-poll, Plow returns HTTP 410 and the helper prints
`Activation code expired.` plus the exact re-run command, then exits non-zero —
just run the command again for a fresh code.

## Per-profile activation

For a multi-profile install, activate each profile into its own
`data/profiles/<name>/.env` with `--profile`:

```bash
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel-team
```

Each run prints `Profile <name> activated. Wrote PLOW_CHAT_CHAT_UID +
PLOW_CHAT_TOKEN to <path>.` on success. Confirm with:

```bash
# Expect 2 — -c prints the match count, not the secret values (see SEED.md).
grep -cE '^PLOW_CHAT_(CHAT_UID|TOKEN)=' hermes-agent/data/profiles/daniel/.env
```

## Non-interactive test mode (DinD/CI)

Phase 4 normally needs a human texting the code from the target iPhone, which a
headless DinD/CI run cannot do. For test validation only, `--test-mode` writes
operator-supplied credentials to the profile `.env` and skips the phone-bind
dance entirely (it never contacts Plow):

```bash
ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel \
  --test-mode --test-chat-uid cht_known --test-token tok_known
```

The audit file records `"status": "test-mode"`. Never use this for a real
operator install.
