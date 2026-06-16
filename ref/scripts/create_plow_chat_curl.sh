#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${PLOW_CHAT_BASE_URL:-https://api.plow.co}"
SCAFFOLD_DIR="${HERMES_SCAFFOLD_DIR:-./hermes-agent}"
DATA_DIR="${HERMES_DATA_DIR:-}"
DATA_DIR_EXPLICIT=""
PROFILE="${PLOW_CHAT_PROFILE:-}"
DISPLAY_NAME="${PLOW_CHAT_DISPLAY_NAME:-Hermes user}"
TIMEOUT_SECONDS="${PLOW_CHAT_VERIFY_TIMEOUT:-900}"
POLL_INTERVAL="${PLOW_CHAT_VERIFY_POLL_INTERVAL:-5}"

# Non-interactive test binding (defect #14). When set, the helper skips the
# phone-bind dance and writes operator-supplied credentials straight to the
# profile env. For testing/CI only — never for real operator activation.
TEST_MODE=""
TEST_CHAT_UID="${PLOW_CHAT_TEST_CHAT_UID:-}"
TEST_TOKEN="${PLOW_CHAT_TEST_TOKEN:-}"

# --env-file: write the PLOW_CHAT_* vars to an explicit path instead of the
# <data-dir>/.env computed from --scaffold/--profile/--data-dir. Used by the
# up-front prepare-script to land creds in the openseed inputs file before any
# on-Pi scaffold exists. PLOW_CHAT_ENV_FILE is the env override.
ENV_FILE_EXPLICIT="${PLOW_CHAT_ENV_FILE:-}"

# --from-env: place already-obtained creds (from a real prior phone-bind) and
# skip the phone-bind dance entirely. A first-class operator path, distinct from
# --from-env: reads PLOW_CHAT_TOKEN/PLOW_CHAT_CHAT_UID (+ optional
# PLOW_CHAT_BASE_URL) and records status:"preset". Home channel = chat uid
# (same as the live and test paths — no separate override knob).
FROM_ENV=""
FROM_ENV_TOKEN="${PLOW_CHAT_TOKEN:-}"
FROM_ENV_CHAT_UID="${PLOW_CHAT_CHAT_UID:-}"
# Whether the best-effort activation audit actually landed (gates the success line).
AUDIT_WRITTEN=0

# Initialized up-front so they are always defined under `set -u`, even on the
# test-mode path where the live activation block never runs.
DISPLAY_CODE=""
ACTIVATION_SECRET=""
SEND_TO=""
LINE_ID=""
REDEEM_JSON=""
REDEEM_HTTP_CODE=""

usage() {
  cat <<'EOF'
Usage: ref/scripts/create_plow_chat_curl.sh [options]

Starts Plow activation with provision_chat=true, prints the activation message,
polls activation redeem until verified, then writes PLOW_CHAT_* to the target
profile's .env and a redacted .activation.json audit file.

Options:
  --scaffold PATH        seed-hermes scaffold directory, default ./hermes-agent
  --profile NAME         Write to <scaffold>/data/profiles/<NAME>/.env
  --data-dir PATH        Explicit Hermes data directory override (wins over --profile)
  --env-file PATH        Write PLOW_CHAT_* to exactly PATH (no scaffold required);
                         wins over --scaffold/--profile/--data-dir. For the
                         up-front prepare-script writing to the openseed inputs
                         file before any on-Pi scaffold exists.
  --from-env             Place already-obtained creds and skip the phone-bind.
                         Reads PLOW_CHAT_TOKEN + PLOW_CHAT_CHAT_UID (and optional
                         PLOW_CHAT_BASE_URL) from the env; home channel = chat uid.
                         A real, supported operator path (records status:"preset").
  --base-url URL         Plow API base URL, default https://api.plow.co
  --display-name NAME    Session display name, default "Hermes user"
  --timeout SECONDS      Poll timeout, default 900
  --interval SECONDS     Poll interval, default 5

Testing only (skips the phone-bind activation, see SEED.md):
  --test-mode            Write operator-supplied credentials, skip activation
  --test-chat-uid UID    PLOW_CHAT_CHAT_UID value for --test-mode
  --test-token TOKEN     PLOW_CHAT_TOKEN value for --test-mode

Environment overrides:
  HERMES_SCAFFOLD_DIR
  HERMES_DATA_DIR
  PLOW_CHAT_PROFILE
  PLOW_CHAT_BASE_URL
  PLOW_CHAT_DISPLAY_NAME
  PLOW_CHAT_VERIFY_TIMEOUT
  PLOW_CHAT_VERIFY_POLL_INTERVAL
  PLOW_CHAT_ENV_FILE             (same as --env-file)
  PLOW_CHAT_TOKEN                (with --from-env)
  PLOW_CHAT_CHAT_UID             (with --from-env)
  PLOW_CHAT_TEST_CHAT_UID        (with --test-mode)
  PLOW_CHAT_TEST_TOKEN           (with --test-mode)

Examples:
  # Activate the owner profile "daniel":
  ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel

  # Up-front phone-bind: run the real activation, write creds to the inputs file:
  ref/scripts/create_plow_chat_curl.sh \
    --env-file ~/.config/seed/seed-life-dashboard-hermes.env

  # Place already-obtained creds into the on-Pi scaffold (no phone-bind):
  PLOW_CHAT_TOKEN=tok_xxx PLOW_CHAT_CHAT_UID=cht_xxx \
    ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel --from-env

  # Non-interactive test binding for DinD/CI (no phone required):
  ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel \
    --test-mode --test-chat-uid cht_xxx --test-token tok_xxx
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scaffold) SCAFFOLD_DIR="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; DATA_DIR_EXPLICIT="1"; shift 2 ;;
    --env-file) ENV_FILE_EXPLICIT="$2"; shift 2 ;;
    --from-env) FROM_ENV="1"; shift ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --display-name) DISPLAY_NAME="$2"; shift 2 ;;
    --timeout) TIMEOUT_SECONDS="$2"; shift 2 ;;
    --interval) POLL_INTERVAL="$2"; shift 2 ;;
    --test-mode) TEST_MODE="1"; shift ;;
    --test-chat-uid) TEST_CHAT_UID="$2"; shift 2 ;;
    --test-token) TEST_TOKEN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

BASE_URL="${BASE_URL%/}"
# Resolve the target data dir: explicit --data-dir wins, then --profile resolves
# to the per-profile data dir verified by the install docs, else scaffold data/.
if [[ -z "$DATA_DIR" ]]; then
  if [[ -n "$PROFILE" ]]; then
    DATA_DIR="${SCAFFOLD_DIR%/}/data/profiles/${PROFILE}"
  else
    DATA_DIR="${SCAFFOLD_DIR%/}/data"
  fi
fi
# --env-file wins over the computed <data-dir>/.env: write to exactly that path
# and land the audit beside it, without requiring a scaffold/data dir to exist.
if [[ -n "$ENV_FILE_EXPLICIT" ]]; then
  ENV_FILE="$ENV_FILE_EXPLICIT"
  ACTIVATION_AUDIT_FILE="$(dirname "$ENV_FILE")/.activation.json"
else
  ENV_FILE="${DATA_DIR%/}/.env"
  ACTIVATION_AUDIT_FILE="${DATA_DIR%/}/.activation.json"
fi

# Human-readable profile label for the success/verification message (defect #16).
if [[ -n "$PROFILE" ]]; then
  PROFILE_LABEL="$PROFILE"
elif [[ "${DATA_DIR%/}" == */profiles/* ]]; then
  PROFILE_LABEL="$(basename "${DATA_DIR%/}")"
else
  PROFILE_LABEL="default"
fi

# Exact command to re-run after an expiry / write failure (defects #13, #15, #16).
# When --env-file won the target, the retry MUST target the same env file — else a
# retry would send fresh credentials to the scaffold-shaped default, not the
# requested inputs file (contract-drift).
if [[ -n "$ENV_FILE_EXPLICIT" ]]; then
  RETRY_CMD="bash ref/scripts/create_plow_chat_curl.sh --env-file ${ENV_FILE_EXPLICIT}"
else
  RETRY_CMD="bash ref/scripts/create_plow_chat_curl.sh --scaffold ${SCAFFOLD_DIR}"
  if [[ -n "$PROFILE" ]]; then
    RETRY_CMD="${RETRY_CMD} --profile ${PROFILE}"
  elif [[ -n "$DATA_DIR_EXPLICIT" ]]; then
    RETRY_CMD="${RETRY_CMD} --data-dir ${DATA_DIR}"
  fi
fi
if [[ "$DISPLAY_NAME" != "Hermes user" ]]; then
  RETRY_CMD="${RETRY_CMD} --display-name '${DISPLAY_NAME}'"
fi
# Preserve --from-env in the retry so a preset-creds write failure reruns the
# preset placement, not the live phone-bind (which --from-env exists to skip).
if [[ -n "$FROM_ENV" ]]; then
  RETRY_CMD="${RETRY_CMD} --from-env"
fi

command -v curl >/dev/null 2>&1 || {
  echo "Missing required command: curl" >&2
  exit 1
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

json_object_value() {
  local json="$1"
  local key="$2"
  printf '%s' "$json" |
    tr '\n' ' ' |
    grep -oE "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" |
    head -n 1 |
    sed -nE "s/\"${key}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\"/\1/p" ||
    true
}

json_chat_value() {
  local json="$1"
  local key="$2"
  local chat
  chat="$(
    printf '%s' "$json" |
      tr '\n' ' ' |
      sed -nE 's/.*"chat"[[:space:]]*:[[:space:]]*(\{.*\}).*/\1/p' |
      sed -E 's/"participants"[[:space:]]*:[[:space:]]*\[[^][]*\]//g' ||
      true
  )"
  json_object_value "$chat" "$key"
}

json_value() {
  local json="$1"
  local jq_expr="$2"
  local key="$3"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$json" | jq -r "$jq_expr // empty" 2>/dev/null || true
    return
  fi
  case "$jq_expr" in
    '.chat.uid')
      json_chat_value "$json" "$key"
      ;;
    *)
      json_object_value "$json" "$key"
      ;;
  esac
}

# Re-apply correct permissions and ensure the data dir is writable BEFORE we try
# to write (defects #15/#16). seed-hermes prepare.sh and the running container
# can churn ownership/mode on the bind-mounted data tree between activation start
# and the verified write. If we still cannot write, EXIT NON-ZERO with a clear,
# actionable error instead of failing opaquely or silently skipping the write.
ensure_data_dir_writable() {
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  if [[ -d "$DATA_DIR" ]]; then
    chmod u+rwx "$DATA_DIR" 2>/dev/null || true
  fi
  if [[ ! -d "$DATA_DIR" || ! -w "$DATA_DIR" ]]; then
    echo "ERROR: profile data directory is not writable: ${DATA_DIR}" >&2
    echo "       The seed-hermes scaffold may have re-owned data/ to the" >&2
    echo "       container user (commonly uid/gid 10000) during prepare.sh or" >&2
    echo "       container start, so this helper cannot save PLOW_CHAT_*." >&2
    echo "       Restore host write access and re-run, e.g.:" >&2
    echo "         sudo chown -R \"\$(id -u)\":\"\$(id -g)\" \"${DATA_DIR}\"" >&2
    echo "       (or run this helper with sufficient privileges), then:" >&2
    echo "         ${RETRY_CMD}" >&2
    exit 73
  fi
}

# --env-file variant: there is no scaffold data dir, so just make sure ENV_FILE's
# parent dir exists and we can create/write ENV_FILE.
ensure_env_file_writable() {
  local dir
  dir="$(dirname "$ENV_FILE")"
  mkdir -p "$dir" 2>/dev/null || true
  if [[ ! -d "$dir" || ! -w "$dir" ]] || { [[ -e "$ENV_FILE" ]] && [[ ! -w "$ENV_FILE" ]]; }; then
    echo "ERROR: env file is not writable: ${ENV_FILE}" >&2
    echo "       Ensure its parent directory exists and is writable, then re-run:" >&2
    echo "         ${RETRY_CMD}" >&2
    exit 73
  fi
}

# Dispatch the right pre-write writability guard for the configured target.
ensure_target_writable() {
  if [[ -n "$ENV_FILE_EXPLICIT" ]]; then
    ensure_env_file_writable
  else
    ensure_data_dir_writable
  fi
}

write_env_var() {
  local key="$1"
  local value="$2"
  local tmp
  mkdir -p "$(dirname "$ENV_FILE")" 2>/dev/null || true
  tmp="$(mktemp)"
  if [[ -f "$ENV_FILE" ]]; then
    awk -F= -v key="$key" '$1 != key { print }' "$ENV_FILE" >"$tmp"
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  if ! mv "$tmp" "$ENV_FILE" 2>/dev/null; then
    rm -f "$tmp"
    echo "ERROR: failed to write ${key} to ${ENV_FILE} (permission denied?)." >&2
    echo "       Restore host write access to $(dirname "$ENV_FILE") and re-run:" >&2
    echo "         ${RETRY_CMD}" >&2
    exit 73
  fi
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

json_object_or_empty() {
  local json="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$json" | jq -c . 2>/dev/null || printf '{}'
    return
  fi
  case "$(printf '%s' "$json" | tr -d '[:space:]' | cut -c1)" in
    '{'|'[') printf '%s' "$json" ;;
    *) printf '{}' ;;
  esac
}

write_activation_audit() {
  local token="$1"
  local chat_uid="$2"
  local owner_identity_json="$3"
  local channels_json="$4"
  local status="${5:-verified}"
  local tmp
  local token_last4="${token: -4}"
  mkdir -p "$(dirname "$ACTIVATION_AUDIT_FILE")" 2>/dev/null || true
  tmp="$(mktemp)"
  cat >"$tmp" <<EOF
{
  "base_url": "$(json_escape "$BASE_URL")",
  "verified_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "profile": "$(json_escape "$PROFILE_LABEL")",
  "activation": {
    "display_code": "$(json_escape "$DISPLAY_CODE")",
    "activation_secret": "<redacted>",
    "send_to": "$(json_escape "$SEND_TO")",
    "line_id": "$(json_escape "$LINE_ID")"
  },
  "redeem": {
    "status": "$(json_escape "$status")",
    "token_last4": "$(json_escape "$token_last4")",
    "chat_uid": "$(json_escape "$chat_uid")"
  },
  "owner_identity": $(json_object_or_empty "$owner_identity_json"),
  "channels": $(json_object_or_empty "$channels_json")
}
EOF
  if ! mv "$tmp" "$ACTIVATION_AUDIT_FILE" 2>/dev/null; then
    rm -f "$tmp"
    # When writing to an explicit --env-file (e.g. the openseed inputs file), the
    # audit is best-effort: a failure to write it MUST NOT fail the run, since the
    # PLOW_CHAT_* creds — the deliverable — already landed in ENV_FILE.
    if [[ -n "$ENV_FILE_EXPLICIT" ]]; then
      echo "WARN: could not write activation audit ${ACTIVATION_AUDIT_FILE}; continuing." >&2
      AUDIT_WRITTEN=0
      return 0
    fi
    echo "ERROR: failed to write activation audit ${ACTIVATION_AUDIT_FILE}." >&2
    exit 73
  fi
  chmod 600 "$ACTIVATION_AUDIT_FILE" 2>/dev/null || true
  AUDIT_WRITTEN=1
}

# Print the verification message that lets an operator confirm Phase 4 succeeded
# without manually opening the profile env file (defect #16).
print_activation_success() {
  local chat_uid="$1"
  echo "Chat uid: ${chat_uid}"
  echo "Profile ${PROFILE_LABEL} activated. Wrote PLOW_CHAT_CHAT_UID + PLOW_CHAT_TOKEN to ${ENV_FILE}."
  if [[ "$AUDIT_WRITTEN" == 1 ]]; then
    echo "Wrote redacted activation audit to ${ACTIVATION_AUDIT_FILE}"
  else
    echo "(activation audit not written — best-effort under --env-file; the PLOW_CHAT_* creds landed in ${ENV_FILE})"
  fi
}

# Single writer for the PLOW_CHAT_* contract, shared by all three credential
# placement paths (live activation, --from-env, --test-mode): make the target
# writable, write the four env keys (home channel = chat uid), record the audit,
# and print the success message — one place to keep the contract right.
place_credentials() {
  local token="$1" chat_uid="$2" status="$3" owner_json="$4" channels_json="$5"
  ensure_target_writable
  write_env_var "PLOW_CHAT_BASE_URL" "$BASE_URL"
  write_env_var "PLOW_CHAT_CHAT_UID" "$chat_uid"
  write_env_var "PLOW_CHAT_TOKEN" "$token"
  write_env_var "PLOW_CHAT_HOME_CHANNEL" "$chat_uid"
  write_activation_audit "$token" "$chat_uid" "$owner_json" "$channels_json" "$status"
  print_activation_success "$chat_uid"
}

# POST the redeem payload, capturing both the response body and the HTTP status
# code WITHOUT -f so a non-2xx (e.g. 410 expired) does not abort the script with
# an opaque `curl: (22)` (defect #13). Sets REDEEM_JSON and REDEEM_HTTP_CODE.
redeem_once() {
  local body_file code
  body_file="$(mktemp)"
  code="$(printf '%s' "$REDEEM_PAYLOAD" | curl -sSL \
    -H 'Content-Type: application/json' \
    -d @- \
    -o "$body_file" \
    -w '%{http_code}' \
    "${BASE_URL}/v1/auth/activate/redeem")" || code="000"
  REDEEM_HTTP_CODE="$code"
  REDEEM_JSON="$(cat "$body_file")"
  rm -f "$body_file"
}

# GET a JSON surface with the verified Bearer token. The auth header is fed to
# curl via --config on stdin so the user-wide token never appears in argv where
# a local `ps` could read it (defect #13 / see SEED.md). $1 = URL; prints the
# response body, or '{}' on any failure (these snapshots are best-effort).
get_with_token() {
  printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
    | curl -fsSL --config - "$1" 2>/dev/null || printf '{}'
}

# --- Non-interactive test binding (defect #14): skip the phone-bind dance. ----
if [[ -n "$TEST_MODE" ]]; then
  if [[ -z "$TEST_CHAT_UID" || -z "$TEST_TOKEN" ]]; then
    echo "ERROR: --test-mode requires a chat uid and token." >&2
    echo "       Provide --test-chat-uid <uid> --test-token <token>" >&2
    echo "       (or PLOW_CHAT_TEST_CHAT_UID / PLOW_CHAT_TEST_TOKEN)." >&2
    exit 2
  fi
  echo "TEST MODE: skipping Plow phone-bind activation (testing only)."
  place_credentials "$TEST_TOKEN" "$TEST_CHAT_UID" "test-mode" '{}' '{}'
  exit 0
fi

# --- Preset creds (--from-env): place already-obtained creds from a real prior --
# phone-bind and skip the phone-bind dance. A first-class, supported operator
# path (unlike --test-mode): used to land creds the operator already obtained
# up front into the on-Pi scaffold at install time. Records status:"preset".
if [[ -n "$FROM_ENV" ]]; then
  if [[ -z "$FROM_ENV_TOKEN" || -z "$FROM_ENV_CHAT_UID" ]]; then
    echo "ERROR: --from-env requires PLOW_CHAT_TOKEN and PLOW_CHAT_CHAT_UID in the environment." >&2
    echo "       Export both (from a prior phone-bind) and re-run, e.g.:" >&2
    echo "         PLOW_CHAT_TOKEN=tok_xxx PLOW_CHAT_CHAT_UID=cht_xxx ${RETRY_CMD}" >&2
    exit 2
  fi
  echo "Placing preset Plow chat credentials (skipping phone-bind activation)."
  place_credentials "$FROM_ENV_TOKEN" "$FROM_ENV_CHAT_UID" "preset" '{}' '{}'
  exit 0
fi

# Fail fast if we cannot write the profile env BEFORE asking a human to text the
# activation code, so the operator never completes the phone-bind only to hit a
# write failure afterward (defect #15).
ensure_target_writable

PAYLOAD="$(printf '{"name":"%s","provision_chat":true}' "$(json_escape "$DISPLAY_NAME")")"

echo "Starting Plow activation..."
if ! ACTIVATION_JSON="$(curl -fsSL \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" \
  "${BASE_URL}/v1/auth/activate")"; then
  echo "Failed to start Plow activation against ${BASE_URL}." >&2
  echo "Check connectivity to the Plow API and re-run:" >&2
  echo "  ${RETRY_CMD}" >&2
  exit 1
fi

DISPLAY_CODE="$(json_value "$ACTIVATION_JSON" '.display_code' 'display_code')"
ACTIVATION_SECRET="$(json_value "$ACTIVATION_JSON" '.activation_secret' 'activation_secret')"
SEND_TO="$(json_value "$ACTIVATION_JSON" '.send_to' 'send_to')"
LINE_ID="$(json_value "$ACTIVATION_JSON" '.line_id' 'line_id')"

if [[ -z "$DISPLAY_CODE" || -z "$ACTIVATION_SECRET" || -z "$SEND_TO" ]]; then
  echo "Could not parse display code, activation secret, or send_to from Plow activation response." >&2
  echo "Response was saved nowhere to avoid leaking activation credentials." >&2
  exit 1
fi

echo
echo "Plow activation started."
if [[ -n "$LINE_ID" ]]; then
  echo "Line uid: ${LINE_ID}"
fi
echo "Text Plow Activate: ${DISPLAY_CODE} from iMessage to ${SEND_TO}"
echo

echo "Polling activation redeem until verified..."
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
last_status=""
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  REDEEM_PAYLOAD="$(printf '{"activation_secret":"%s"}' "$(json_escape "$ACTIVATION_SECRET")")"
  redeem_once
  case "$REDEEM_HTTP_CODE" in
    410)
      # The activation secret/code expired before the text arrived.
      echo "Activation code expired. The displayed code is single-use and time-limited." >&2
      echo "Run again to get a fresh code:" >&2
      echo "  ${RETRY_CMD}" >&2
      exit 75
      ;;
    2??)
      : # parse status below
      ;;
    *)
      echo "Activation redeem failed (HTTP ${REDEEM_HTTP_CODE})." >&2
      echo "Run again to get a fresh code:" >&2
      echo "  ${RETRY_CMD}" >&2
      exit 75
      ;;
  esac
  STATUS="$(json_value "$REDEEM_JSON" '.status' 'status')"
  if [[ "$STATUS" != "$last_status" ]]; then
    echo "Status: ${STATUS:-unknown}"
    last_status="$STATUS"
  fi
  if [[ "$STATUS" == "verified" ]]; then
    TOKEN="$(json_value "$REDEEM_JSON" '.token' 'token')"
    CHAT_UID="$(json_value "$REDEEM_JSON" '.chat.uid' 'uid')"
    if [[ -z "$TOKEN" || -z "$CHAT_UID" ]]; then
      echo "Activation verified, but redeem did not include both token and chat uid." >&2
      exit 1
    fi
    # Fail fast on a token carrying quote/backslash/CR/LF: a real Plow bearer
    # token never does, and such chars would break out of get_with_token's
    # curl --config "header = \"...\"" line (config-injection).
    if [[ "$TOKEN" == *[$'\r\n"\\']* ]]; then
      echo "Redeem token contains unexpected control or quote characters; refusing to proceed." >&2
      exit 1
    fi
    OWNER_IDENTITY_JSON="$(get_with_token "${BASE_URL}/v1/auth/owner-identity")"
    CHANNELS_JSON="$(get_with_token "${BASE_URL}/v1/me/channels")"
    # Re-apply permissions right before writing: the container may have churned
    # data/ ownership during the poll window (defect #16).
    echo "Verified: chat is active."
    place_credentials "$TOKEN" "$CHAT_UID" "verified" "$OWNER_IDENTITY_JSON" "$CHANNELS_JSON"
    exit 0
  fi
  sleep "$POLL_INTERVAL"
done

echo "Timed out waiting for activation after ${TIMEOUT_SECONDS}s." >&2
echo "If the activation code expired, start activation again for a new code:" >&2
echo "  ${RETRY_CMD}" >&2
exit 124
