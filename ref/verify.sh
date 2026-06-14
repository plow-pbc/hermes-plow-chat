#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

# 1. Adapter shape + compile. Python is a maintainer verification tool here;
# the documented host install/orchestration path is shell + curl only.
python3 -m py_compile \
  __init__.py \
  ref/hermes-plugin/plow_chat/adapter.py

python3 - <<'PY'
import pathlib
adapter = pathlib.Path('ref/hermes-plugin/plow_chat/adapter.py').read_text()
for needed in ['class PlowChatAdapter', 'def connect', 'def disconnect', 'def send', 'def register(']:
    if needed not in adapter:
        raise SystemExit(f'adapter.py missing: {needed}')
if '"plow_chat"' not in adapter and "'plow_chat'" not in adapter:
    raise SystemExit('adapter.py does not register platform name plow_chat')
PY

# 2. Direct-mount file-set + config enablement check.
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/hermes-agent"
PLOW_CHAT_PLUGIN_LOCAL_DIR=. ref/scripts/install_direct_mount.sh --scaffold "$tmpdir/hermes-agent" >/tmp/seed-hermes-plow-install.out

for path in \
  "$tmpdir/hermes-agent/data/plugins/plow-chat-platform/plugin.yaml" \
  "$tmpdir/hermes-agent/data/plugins/plow-chat-platform/__init__.py" \
  "$tmpdir/hermes-agent/data/plugins/plow-chat-platform/ref/hermes-plugin/plow_chat/adapter.py"
do
  [[ -f "$path" ]] || { echo "missing direct-mounted file: $path" >&2; exit 1; }
done

grep -q 'plow-chat-platform' "$tmpdir/hermes-agent/data/config.yaml" || {
  echo 'config.yaml does not enable plow-chat-platform' >&2
  exit 1
}
cat >"$tmpdir/hermes-agent/data/config.yaml" <<'YAML'
plugins:
  enabled: [other-plugin]
  disabled:
    - plow-chat-platform
    - keep-disabled
terminal:
  cwd: /opt/data/workspace
YAML
PLOW_CHAT_PLUGIN_LOCAL_DIR=. ref/scripts/install_direct_mount.sh --scaffold "$tmpdir/hermes-agent" >/tmp/seed-hermes-plow-install-existing.out
grep -q 'enabled: \[other-plugin, plow-chat-platform\]' "$tmpdir/hermes-agent/data/config.yaml" || {
  echo 'config.yaml inline enabled list was not preserved and extended' >&2
  exit 1
}
if awk '/disabled:/{in_disabled=1; next} in_disabled && /^  [^ ]/{in_disabled=0} in_disabled && /plow-chat-platform/{found=1} END{exit found ? 0 : 1}' "$tmpdir/hermes-agent/data/config.yaml"; then
  echo 'config.yaml still disables plow-chat-platform after install' >&2
  exit 1
fi

# 3. Host shell helpers are syntax-valid and contain no Python/git/Hermes CLI dependency.
bash -n ref/scripts/install_direct_mount.sh ref/scripts/create_plow_chat_curl.sh ref/scripts/install_connectors.sh
if [[ -e after-install.md || -e ref/scripts/bootstrap_fresh_hermes.sh || -e ref/scripts/configure_hermes_env.py ]]; then
  echo 'old host installer artifact still exists' >&2
  exit 1
fi
if grep -rnE 'python3|git clone|hermes plugins|hermes gateway|GH[_]TOKEN|PLOW_CHAT_LINE|--line' ref/scripts; then
  echo 'host scripts still reference Python/git/Hermes installer artifacts' >&2
  exit 1
fi

# 4. jq-less curl orchestration check. The host path guarantees curl, not jq;
# keep jq out of PATH and verify optional missing fields do not abort parsing.
mockdir="$(mktemp -d)"
mkdir -p "$mockdir/bin" "$mockdir/hermes-agent"
for cmd in bash tr grep head sed mktemp mkdir awk mv rm chmod date sleep dirname basename cat cp cut; do
  target="$(command -v "$cmd")"
  ln -s "$target" "$mockdir/bin/$cmd"
done
cat >"$mockdir/bin/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
# Faithful enough to exercise the helper's body+status capture: honor -o <file>
# (write body there) and -w (print the http code to stdout, like %{http_code}).
out=""
want_code=0
args=("$@")
n=${#args[@]}
for ((i=0; i<n; i++)); do
  case "${args[$i]}" in
    -o) out="${args[$((i+1))]}" ;;
    -w) want_code=1 ;;
  esac
done
url="${args[$((n-1))]}"
emit() {  # $1 = body, $2 = http code
  if [[ -n "$out" ]]; then printf '%s' "$1" >"$out"; else printf '%s\n' "$1"; fi
  if [[ "$want_code" -eq 1 ]]; then printf '%s' "$2"; fi
}
case "$url" in
  */v1/auth/activate)
    emit '{"display_code":"ABCDE","activation_secret":"act_test","send_to":"+15551234567","line_id":"ln_test"}' 200
    ;;
  */v1/auth/activate/redeem)
    code="${PLOW_FAKE_REDEEM_CODE:-200}"
    if [[ "$code" != "200" ]]; then
      emit '{"error":"gone"}' "$code"
      exit 0
    fi
    count_file="${PLOW_FAKE_COUNT_FILE:?}"
    count=0
    [[ -f "$count_file" ]] && count="$(cat "$count_file")"
    count=$((count + 1))
    printf '%s' "$count" >"$count_file"
    if [[ "$count" -lt 2 ]]; then
      emit '{"status":"pending"}' 200
    else
      emit '{"status":"verified","token":"token_test","chat":{"uid":"cht_test","status":"active","participants":[{"type":"member","status":"active"}]}}' 200
    fi
    ;;
  */v1/auth/owner-identity)
    emit '{"display_name":"Test Owner","phones":["+15551234567"],"emails":["owner@example.test"]}' 200
    ;;
  */v1/me/channels)
    emit '{"channels":[{"provider":"linq","provider_key":"+15551234567"}]}' 200
    ;;
  *)
    echo "unexpected url: $url" >&2
    exit 2
    ;;
esac
SH
chmod +x "$mockdir/bin/curl"
PATH="$mockdir/bin" PLOW_FAKE_COUNT_FILE="$mockdir/count" \
  bash ref/scripts/create_plow_chat_curl.sh \
    --scaffold "$mockdir/hermes-agent" \
    --base-url https://chat.plow.test \
    --interval 0 \
    --timeout 3 >"$mockdir/out.txt"
grep -q 'Verified: chat is active.' "$mockdir/out.txt" || {
  echo 'jq-less curl orchestration did not poll to active' >&2
  exit 1
}
grep -q 'PLOW_CHAT_CHAT_UID=cht_test' "$mockdir/hermes-agent/data/.env" || {
  echo 'jq-less curl orchestration wrote wrong chat uid' >&2
  exit 1
}
grep -q 'PLOW_CHAT_TOKEN=token_test' "$mockdir/hermes-agent/data/.env" || {
  echo 'jq-less curl orchestration wrote wrong token' >&2
  exit 1
}
grep -q 'Text Plow Activate: ABCDE from iMessage to +15551234567' "$mockdir/out.txt" || {
  echo 'jq-less curl orchestration did not surface the selected line phone number' >&2
  exit 1
}
grep -q 'Profile default activated. Wrote PLOW_CHAT_CHAT_UID + PLOW_CHAT_TOKEN to' "$mockdir/out.txt" || {
  echo 'helper did not print the success verification message' >&2
  exit 1
}
if [[ -e "$mockdir/hermes-agent/data/plow_chat_state.json" ]]; then
  echo 'curl orchestration wrote secret-bearing sidecar state' >&2
  exit 1
fi
if grep -q 'Code expires at:' "$mockdir/out.txt"; then
  echo 'jq-less curl orchestration surfaced missing optional expiry' >&2
  exit 1
fi
if [[ ! -f "$mockdir/hermes-agent/data/.activation.json" ]]; then
  echo 'curl orchestration did not write activation audit' >&2
  exit 1
fi
grep -q '"activation_secret": "<redacted>"' "$mockdir/hermes-agent/data/.activation.json" || {
  echo 'activation audit did not redact activation secret' >&2
  exit 1
}
grep -q '"token_last4": "test"' "$mockdir/hermes-agent/data/.activation.json" || {
  echo 'activation audit did not record token last four' >&2
  exit 1
}
grep -q '"chat_uid": "cht_test"' "$mockdir/hermes-agent/data/.activation.json" || {
  echo 'activation audit did not record chat uid' >&2
  exit 1
}
grep -q '"owner_identity": {"display_name":"Test Owner","phones":\["+15551234567"\],"emails":\["owner@example.test"\]}' "$mockdir/hermes-agent/data/.activation.json" || {
  echo 'activation audit did not record owner identity snapshot' >&2
  exit 1
}
if grep -q 'act_test\|token_test' "$mockdir/hermes-agent/data/.activation.json"; then
  echo 'activation audit leaked full activation secret or token' >&2
  exit 1
fi

# 4b. Per-profile data-dir resolution (defect #12). --profile writes to
# data/profiles/<name>/.env and the success message names the profile.
prof_count="$(mktemp)"
PATH="$mockdir/bin" PLOW_FAKE_COUNT_FILE="$prof_count" \
  bash ref/scripts/create_plow_chat_curl.sh \
    --scaffold "$mockdir/hermes-agent" \
    --profile daniel \
    --base-url https://chat.plow.test \
    --interval 0 --timeout 3 >"$mockdir/out-profile.txt"
grep -q 'PLOW_CHAT_TOKEN=token_test' "$mockdir/hermes-agent/data/profiles/daniel/.env" || {
  echo '--profile did not write to data/profiles/daniel/.env' >&2
  exit 1
}
grep -q 'Profile daniel activated. Wrote PLOW_CHAT_CHAT_UID + PLOW_CHAT_TOKEN to' "$mockdir/out-profile.txt" || {
  echo '--profile success message did not name the profile' >&2
  exit 1
}

# 4c. Activation 410 expiry is actionable (defect #13): no raw curl error, a
# human-readable expiry line, a retry command, and a non-zero exit.
exp_count="$(mktemp)"
set +e
PATH="$mockdir/bin" PLOW_FAKE_COUNT_FILE="$exp_count" PLOW_FAKE_REDEEM_CODE=410 \
  bash ref/scripts/create_plow_chat_curl.sh \
    --scaffold "$mockdir/hermes-agent" \
    --profile expiry \
    --base-url https://chat.plow.test \
    --interval 0 --timeout 3 >"$mockdir/out-410.txt" 2>&1
expiry_rc=$?
set -e
[[ "$expiry_rc" -ne 0 ]] || { echo '410 expiry did not exit non-zero' >&2; exit 1; }
grep -qi 'activation code expired' "$mockdir/out-410.txt" || {
  echo '410 expiry did not print an actionable expired message' >&2
  exit 1
}
grep -q 'create_plow_chat_curl.sh --scaffold .* --profile expiry' "$mockdir/out-410.txt" || {
  echo '410 expiry did not print a retry command' >&2
  exit 1
}
if grep -qi 'curl: (22)' "$mockdir/out-410.txt"; then
  echo '410 expiry leaked the raw curl (22) error' >&2
  exit 1
fi

# 4d. Non-interactive test mode (defect #14): no curl/phone-bind, writes the
# operator-supplied credentials, prints the verification message.
PATH="$mockdir/bin" \
  bash ref/scripts/create_plow_chat_curl.sh \
    --scaffold "$mockdir/hermes-agent" \
    --profile testmode \
    --test-mode --test-chat-uid cht_supplied --test-token tok_supplied \
    >"$mockdir/out-test.txt"
grep -q 'PLOW_CHAT_CHAT_UID=cht_supplied' "$mockdir/hermes-agent/data/profiles/testmode/.env" || {
  echo '--test-mode did not write supplied chat uid' >&2
  exit 1
}
grep -q 'PLOW_CHAT_TOKEN=tok_supplied' "$mockdir/hermes-agent/data/profiles/testmode/.env" || {
  echo '--test-mode did not write supplied token' >&2
  exit 1
}
grep -q 'Profile testmode activated' "$mockdir/out-test.txt" || {
  echo '--test-mode did not print the verification message' >&2
  exit 1
}
grep -q '"status": "test-mode"' "$mockdir/hermes-agent/data/profiles/testmode/.activation.json" || {
  echo '--test-mode audit did not record test-mode status' >&2
  exit 1
}
# --test-mode without credentials must fail with a usage error, not write.
set +e
PATH="$mockdir/bin" bash ref/scripts/create_plow_chat_curl.sh \
  --scaffold "$mockdir/hermes-agent" --profile testmode2 --test-mode \
  >"$mockdir/out-test-bad.txt" 2>&1
test_bad_rc=$?
set -e
[[ "$test_bad_rc" -ne 0 ]] || { echo '--test-mode without creds did not fail' >&2; exit 1; }

# 4e. Write-permission failure is loud and non-zero (defects #15/#16): a
# read-only data dir must abort with a clear remediation, not silently skip.
if [[ "$(id -u)" -ne 0 ]]; then
  ro_root="$(mktemp -d)"
  mkdir -p "$ro_root/data"
  chmod 500 "$ro_root/data"
  perm_count="$(mktemp)"
  set +e
  PATH="$mockdir/bin" PLOW_FAKE_COUNT_FILE="$perm_count" \
    bash ref/scripts/create_plow_chat_curl.sh \
      --data-dir "$ro_root/data/profiles/locked" \
      --base-url https://chat.plow.test \
      --interval 0 --timeout 3 >"$mockdir/out-perm.txt" 2>&1
  perm_rc=$?
  set -e
  chmod 700 "$ro_root/data" 2>/dev/null || true
  rm -rf "$ro_root"
  [[ "$perm_rc" -ne 0 ]] || { echo 'write-permission failure did not exit non-zero' >&2; exit 1; }
  grep -qi 'not writable' "$mockdir/out-perm.txt" || {
    echo 'write-permission failure did not print a clear error' >&2
    exit 1
  }
fi

# 5. Root plugin installability check.
python3 - <<'PY'
import pathlib
root = pathlib.Path('.')
manifest = (root / 'plugin.yaml').read_text()
if 'kind: platform' not in manifest:
    raise SystemExit('root plugin.yaml kind is not platform')
if 'name: plow-chat-platform' not in manifest:
    raise SystemExit('root plugin.yaml has unexpected plugin name')
if not (root / '__init__.py').exists():
    raise SystemExit('missing root __init__.py')
text = (root / '__init__.py').read_text()
if 'register' not in text or 'adapter.py' not in text:
    raise SystemExit('root __init__.py does not expose adapter register(ctx)')
if 'raise ImportError' not in text:
    raise SystemExit('root __init__.py does not fail closed when adapter.py is missing')
PY

# 5b. Connector skill compiles and installs into a scaffold's data/skills/.
python3 -m py_compile ref/hermes-skill/plow-connectors/plow_connector.py
conn_tmp="$(mktemp -d)"
trap 'rm -rf "$tmpdir" "$conn_tmp"' EXIT
ref/scripts/install_connectors.sh --scaffold "$conn_tmp/hermes-agent" >/dev/null
for path in \
  "$conn_tmp/hermes-agent/data/skills/plow-connectors/SKILL.md" \
  "$conn_tmp/hermes-agent/data/skills/plow-connectors/plow_connector.py"
do
  [[ -f "$path" ]] || { echo "connector skill not installed: $path" >&2; exit 1; }
done
[[ -x "$conn_tmp/hermes-agent/data/skills/plow-connectors/plow_connector.py" ]] || {
  echo 'installed plow_connector.py is not executable' >&2; exit 1; }

# 6. Secret hygiene check.
python3 - <<'PY'
import pathlib, re
bad = []
paths = [
    pathlib.Path('README.md'),
    pathlib.Path('SEED.md'),
    pathlib.Path('TESTING.md'),
    pathlib.Path('plugin.yaml'),
    pathlib.Path('__init__.py'),
    pathlib.Path('ref'),
]
for path in paths:
    if not path.exists():
        continue
    files = [path] if path.is_file() else [p for p in path.rglob('*') if p.is_file()]
    for file in files:
        text = file.read_text(errors='ignore')
        if re.search(r'plow_[A-Za-z0-9_-]{16,}', text):
            bad.append(f'{file}: literal-looking session token')
        for m in re.finditer(r'Plow Activate: ([A-Z0-9]{5,})', text):
            if m.group(1) != 'ABCDE':
                bad.append(f'{file}: literal-looking activation code')
if bad:
    raise SystemExit('\n'.join(bad))
PY

echo "ok"
