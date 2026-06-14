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
  PLOW_CHAT_TEST_CHAT_UID        (with --test-mode)
  PLOW_CHAT_TEST_TOKEN           (with --test-mode)

Examples:
  # Activate the owner profile "daniel":
  ref/scripts/create_plow_chat_curl.sh --scaffold ./hermes-agent --profile daniel

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
ENV_FILE="${DATA_DIR%/}/.env"
ACTIVATION_AUDIT_FILE="${DATA_DIR%/}/.activation.json"

# Human-readable profile label for the success/verification message (defect #16).
if [[ -n "$PROFILE" ]]; then
  PROFILE_LABEL="$PROFILE"
elif [[ "${DATA_DIR%/}" == */profiles/* ]]; then
  PROFILE_LABEL="$(basename "${DATA_DIR%/}")"
else
  PROFILE_LABEL="default"
fi

# Exact command to re-run after an expiry / write failure (defects #13, #15, #16).
RETRY_CMD="bash ref/scripts/create_plow_chat_curl.sh --scaffold ${SCAFFOLD_DIR}"
if [[ -n "$PROFILE" ]]; then
  RETRY_CMD="${RETRY_CMD} --profile ${PROFILE}"
elif [[ -n "$DATA_DIR_EXPLICIT" ]]; then
  RETRY_CMD="${RETRY_CMD} --data-dir ${DATA_DIR}"
fi
if [[ "$DISPLAY_NAME" != "Hermes user" ]]; then
  RETRY_CMD="${RETRY_CMD} --display-name '${DISPLAY_NAME}'"
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
    echo "ERROR: failed to write activation audit ${ACTIVATION_AUDIT_FILE}." >&2
    exit 73
  fi
  chmod 600 "$ACTIVATION_AUDIT_FILE" 2>/dev/null || true
}

# Print the verification message that lets an operator confirm Phase 4 succeeded
# without manually opening the profile env file (defect #16).
print_activation_success() {
  local chat_uid="$1"
  echo "Chat uid: ${chat_uid}"
  echo "Profile ${PROFILE_LABEL} activated. Wrote PLOW_CHAT_CHAT_UID + PLOW_CHAT_TOKEN to ${ENV_FILE}."
  echo "Wrote redacted activation audit to ${ACTIVATION_AUDIT_FILE}"
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
# a local `ps` could read it (defect #13 / SEED.md:67). $1 = URL; prints the
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
  ensure_data_dir_writable
  write_env_var "PLOW_CHAT_BASE_URL" "$BASE_URL"
  write_env_var "PLOW_CHAT_CHAT_UID" "$TEST_CHAT_UID"
  write_env_var "PLOW_CHAT_TOKEN" "$TEST_TOKEN"
  write_env_var "PLOW_CHAT_HOME_CHANNEL" "$TEST_CHAT_UID"
  write_activation_audit "$TEST_TOKEN" "$TEST_CHAT_UID" '{}' '{}' "test-mode"
  print_activation_success "$TEST_CHAT_UID"
  exit 0
fi

# Fail fast if we cannot write the profile env BEFORE asking a human to text the
# activation code, so the operator never completes the phone-bind only to hit a
# write failure afterward (defect #15).
ensure_data_dir_writable

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
    ensure_data_dir_writable
    write_env_var "PLOW_CHAT_BASE_URL" "$BASE_URL"
    write_env_var "PLOW_CHAT_CHAT_UID" "$CHAT_UID"
    write_env_var "PLOW_CHAT_TOKEN" "$TOKEN"
    write_env_var "PLOW_CHAT_HOME_CHANNEL" "$CHAT_UID"
    write_activation_audit "$TOKEN" "$CHAT_UID" "$OWNER_IDENTITY_JSON" "$CHANNELS_JSON" "verified"
    echo "Verified: chat is active."
    print_activation_success "$CHAT_UID"
    exit 0
  fi
  sleep "$POLL_INTERVAL"
done

echo "Timed out waiting for activation after ${TIMEOUT_SECONDS}s." >&2
echo "If the activation code expired, start activation again for a new code:" >&2
echo "  ${RETRY_CMD}" >&2
exit 124
