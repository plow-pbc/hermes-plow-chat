#!/usr/bin/env bash
set -euo pipefail

SCAFFOLD_DIR="${HERMES_SCAFFOLD_DIR:-./hermes-agent}"
DATA_DIR="${HERMES_DATA_DIR:-}"
PLUGIN_NAME="plow-chat-platform"
PLUGIN_DIR=""
SOURCE_DIR="${PLOW_CHAT_PLUGIN_LOCAL_DIR:-}"
PLUGIN_REF="${PLOW_CHAT_PLUGIN_REF:-main}"
RAW_BASE="${PLOW_CHAT_SEED_RAW_BASE:-https://raw.githubusercontent.com/plow-pbc/seed-hermes-plow/${PLUGIN_REF}}"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ref/scripts/install_direct_mount.sh [--scaffold ./hermes-agent] [--data-dir ./hermes-agent/data]

Places the Plow Chat Hermes plugin directly into:

  <scaffold>/data/plugins/plow-chat-platform/

and ensures <scaffold>/data/config.yaml enables the manifest name
plow-chat-platform. This helper does not call `hermes`, `git`, or the Hermes
plugin installer. It uses PLOW_CHAT_PLUGIN_LOCAL_DIR when supplied, otherwise it
downloads the required file set from PLOW_CHAT_PLUGIN_REF.

Run this installer before first boot, then run create_plow_chat_curl.sh until it
writes PLOW_CHAT_* into data/.env. Start `docker compose up` only after that so
Hermes boots once with the Plow Chat environment already populated.

Environment overrides:
  HERMES_SCAFFOLD_DIR          default ./hermes-agent
  HERMES_DATA_DIR              explicit data dir override
  PLOW_CHAT_PLUGIN_LOCAL_DIR   copy plugin files from a local checkout
  PLOW_CHAT_PLUGIN_REF         branch/SHA for raw GitHub fetch, default main
  PLOW_CHAT_SEED_RAW_BASE      full raw URL override
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scaffold) SCAFFOLD_DIR="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --source-dir|--local-dir) SOURCE_DIR="$2"; shift 2 ;;
    --ref) PLUGIN_REF="$2"; RAW_BASE="https://raw.githubusercontent.com/plow-pbc/seed-hermes-plow/${PLUGIN_REF}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$SOURCE_DIR" && -f "${SCRIPT_ROOT}/plugin.yaml" && -f "${SCRIPT_ROOT}/__init__.py" ]]; then
  SOURCE_DIR="$SCRIPT_ROOT"
fi
if [[ -z "$DATA_DIR" ]]; then
  DATA_DIR="${SCAFFOLD_DIR%/}/data"
fi
PLUGIN_DIR="${DATA_DIR%/}/plugins/${PLUGIN_NAME}"
CONFIG_FILE="${DATA_DIR%/}/config.yaml"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

copy_or_fetch() {
  local rel="$1"
  local dest="${PLUGIN_DIR}/${rel}"
  mkdir -p "$(dirname "$dest")"
  if [[ -n "$SOURCE_DIR" ]]; then
    cp "${SOURCE_DIR%/}/${rel}" "$dest"
  else
    require_cmd curl
    curl -fsSL "${RAW_BASE%/}/${rel}" -o "$dest"
  fi
}

enable_plugin_in_config() {
  mkdir -p "$(dirname "$CONFIG_FILE")"
  if [[ ! -f "$CONFIG_FILE" ]]; then
    cat >"$CONFIG_FILE" <<'EOF'
plugins:
  enabled:
    - plow-chat-platform
  disabled: []
terminal:
  cwd: /opt/data/workspace
EOF
    return
  fi

  local tmp
  tmp="$(mktemp)"
  awk '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      return s
    }
    function leading_ws(s,    m) {
      m = s
      sub(/[^[:space:]].*$/, "", m)
      return m
    }
    function flush_enabled() {
      if (in_enabled && !enabled_has) {
        # Match the indentation of existing list items so the YAML stays valid
        # regardless of whether the scaffold used a 2- or 4-space list style.
        # Default to 4 spaces when the list was empty.
        print (enabled_indent == "" ? "    " : enabled_indent) "- plow-chat-platform"
      }
      in_enabled = 0
    }
    function remove_from_inline_list(line, key,    n, parts, i, item, kept) {
      if (!match(line, /\[[^]]*\]/)) {
        return line
      }
      n = split(substr(line, RSTART + 1, RLENGTH - 2), parts, ",")
      kept = ""
      for (i = 1; i <= n; i++) {
        item = trim(parts[i])
        gsub(/^["\047]|["\047]$/, "", item)
        if (item == "" || item == key) {
          continue
        }
        kept = kept (kept == "" ? "" : ", ") item
      }
      return substr(line, 1, RSTART - 1) "[" kept "]" substr(line, RSTART + RLENGTH)
    }
    function append_to_inline_list(line, key,    before, inside, after) {
      if (line ~ key) {
        return line
      }
      if (!match(line, /\[[^]]*\]/)) {
        return line
      }
      before = substr(line, 1, RSTART)
      inside = trim(substr(line, RSTART + 1, RLENGTH - 2))
      after = substr(line, RSTART + RLENGTH - 1)
      return before (inside == "" ? key : inside ", " key) after
    }
    BEGIN {
      in_plugins = 0
      in_enabled = 0
      in_disabled = 0
      saw_plugins = 0
      saw_enabled = 0
      enabled_has = 0
    }
    /^plugins:[[:space:]]*$/ {
      flush_enabled()
      saw_plugins = 1
      in_plugins = 1
      in_disabled = 0
      print
      next
    }
    in_plugins && /^[^[:space:]][^:]*:/ {
      flush_enabled()
      if (!saw_enabled) {
        print "  enabled:"
        print "    - plow-chat-platform"
      }
      in_plugins = 0
      in_disabled = 0
    }
    in_plugins && /^  enabled:[[:space:]]*\[/ {
      flush_enabled()
      saw_enabled = 1
      print append_to_inline_list($0, "plow-chat-platform")
      next
    }
    in_plugins && /^  enabled:[[:space:]]*$/ {
      flush_enabled()
      saw_enabled = 1
      in_enabled = 1
      enabled_has = 0
      enabled_indent = ""
      in_disabled = 0
      print
      next
    }
    # A block list item naming the plugin, at ANY indent. Under enabled: it
    # marks the plugin already present (idempotent: no duplicate append) and
    # records the item indent; under disabled: it is dropped so re-enabling wins.
    in_plugins && /^[[:space:]]+-[[:space:]]*plow-chat-platform[[:space:]]*$/ {
      if (in_enabled) {
        enabled_has = 1
        enabled_indent = leading_ws($0)
      }
      if (in_disabled) {
        next
      }
      print
      next
    }
    # Any other block list item under enabled: records the sibling indent so an
    # appended entry lines up with it.
    in_enabled && /^[[:space:]]+-/ {
      enabled_indent = leading_ws($0)
      print
      next
    }
    # First non-list-item line ends the enabled block; flush any pending append.
    in_enabled && !/^[[:space:]]+-/ {
      flush_enabled()
    }
    in_plugins && /^  disabled:[[:space:]]*\[/ {
      in_disabled = 0
      print remove_from_inline_list($0, "plow-chat-platform")
      next
    }
    in_plugins && /^  disabled:[[:space:]]*$/ {
      in_disabled = 1
      print
      next
    }
    in_plugins && /^  [^[:space:]][^:]*:/ {
      in_disabled = 0
    }
    { print }
    END {
      flush_enabled()
      if (in_plugins && !saw_enabled) {
        print "  enabled:"
        print "    - plow-chat-platform"
      }
      if (!saw_plugins) {
        print ""
        print "plugins:"
        print "  enabled:"
        print "    - plow-chat-platform"
        print "  disabled: []"
      }
    }
  ' "$CONFIG_FILE" >"$tmp"
  mv "$tmp" "$CONFIG_FILE"
}

mkdir -p "$PLUGIN_DIR"
copy_or_fetch "plugin.yaml"
copy_or_fetch "__init__.py"
copy_or_fetch "ref/hermes-plugin/plow_chat/adapter.py"

mkdir -p "${DATA_DIR%/}/workspace"
enable_plugin_in_config

# Per-profile plugin visibility.
# Hermes (as of 0.14.0) discovers plugins from each profile's own plugins/ dir.
# The scaffold-root data/plugins/ install only registers the plugin in the
# DEFAULT profile context. For multi-profile installs (e.g. one owner profile +
# one team-listener profile, both needing plow_chat), symlink the plugin dir
# into each existing profile so `hermes -p <profile> plugins list` finds it.
PROFILES_DIR="${DATA_DIR%/}/profiles"
if [[ -d "$PROFILES_DIR" ]]; then
  for prof_dir in "$PROFILES_DIR"/*/; do
    [[ -d "$prof_dir" ]] || continue
    prof_name=$(basename "${prof_dir%/}")
    prof_plugins="${prof_dir}plugins"
    mkdir -p "$prof_plugins"
    target="${prof_plugins}/${PLUGIN_NAME}"
    if [[ ! -e "$target" ]]; then
      # Relative path so the symlink survives bind-mount path changes.
      ln -sfn "../../../plugins/${PLUGIN_NAME}" "$target"
      echo "  + symlinked ${PLUGIN_NAME} into profile '${prof_name}' plugins/"
    fi
  done
fi

echo "Installed ${PLUGIN_NAME} into ${PLUGIN_DIR}"
echo "Enabled ${PLUGIN_NAME} in ${CONFIG_FILE}"
