#!/usr/bin/env python3
"""Compression must cut tags, and must never corrupt a payload."""
import os
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from npm_c2.crypto import compress, decompress, COMPRESS_MIN       # noqa: E402
from npm_c2 import encode_payload, RES_PREFIX                      # noqa: E402

fails = []

# Round-trip across the interesting sizes.
for size in (0, 1, 63, 64, 65, 500, 1320, 12000):
    payload = bytes((i * 7 + 13) % 256 for i in range(size))
    if decompress(compress(payload)) != payload:
        fails.append(f"round-trip failed at {size} bytes")
if not fails:
    print("[PASS] round-trip across 0..12000 bytes")

# Realistic Windows output compresses hard.
sample = b"Volume in drive C has no label.\r\n" * 40
raw_tags = len(encode_payload(sample, RES_PREFIX))
cmp_tags = len(encode_payload(compress(sample), RES_PREFIX))
if cmp_tags >= raw_tags:
    fails.append(f"compression did not reduce tags ({raw_tags} -> {cmp_tags})")
else:
    print(f"[PASS] 1.3KB output: {raw_tags} tags -> {cmp_tags} tag(s)")

# Below the threshold the payload is stored raw, with a one-byte marker.
small = b"whoami"
packed = compress(small)
if packed[:1] != b"\x00" or packed[1:] != small:
    fails.append("small payload was not stored raw")
else:
    print(f"[PASS] {len(small)}B stored raw (+1 marker byte)")

# Incompressible data must not grow beyond one marker byte.
import os as _os
random_blob = _os.urandom(400)
packed = compress(random_blob)
if len(packed) > len(random_blob) + 1:
    fails.append(f"incompressible data grew to {len(packed)}")
else:
    print(f"[PASS] incompressible data stored raw ({len(packed)}B)")

# A legacy tag with no marker must still decode unchanged.
legacy = b"raw old payload"
if decompress(legacy) != legacy:
    fails.append("legacy payload without a marker was mangled")
else:
    print("[PASS] legacy payload without a marker passes through")

# zlib verdict: what compression actually buys in tags per reply size.
print("\nreply size   raw tags   compressed tags")
for size in (200, 500, 1320, 4000, 12000):
    body = (b"ERROR: something failed at line 42\r\n" * (size // 36 + 1))[:size]
    print(f"{size:>10}   {len(encode_payload(body, RES_PREFIX)):>8}   "
          f"{len(encode_payload(compress(body), RES_PREFIX)):>15}")

print(f"\nthreshold: {COMPRESS_MIN} bytes")
print()
if fails:
    for f in fails:
        print("FAIL:", f)
    sys.exit(1)
print("compression checks passed")
